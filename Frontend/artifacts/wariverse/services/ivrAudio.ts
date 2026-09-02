/**
 * Audio engine for the IVR call screen: play prompts, record push-to-talk.
 *
 * ── On `expo-av` ────────────────────────────────────────────────────────────
 * `expo-av` is NOT currently in this app's package.json, and every other audio
 * path here (see services/speechService.ts) uses the browser APIs that
 * react-native-web provides. So this module does both:
 *
 *   1. If `expo-av` is installed it is picked up at runtime and used, which is
 *      what a native build needs.
 *   2. Otherwise it falls back to `Audio`/`MediaRecorder`, which is what
 *      actually runs today on web and in Expo Go.
 *
 * The require is deliberately indirect so the bundler cannot fail the build on
 * a package that is not there. To enable the native path:
 *
 *     pnpm --filter @workspace/wariverse add expo-av
 *
 * (In SDK 54 `expo-audio` supersedes `expo-av`; `loadExpoAv()` is the single
 * place to swap.)
 */

import { speakOnDevice, stopSpeaking } from '@/services/speechService';
import type { Language } from '@/types/domain';

type ExpoAvModule = any;

// Declared locally so this file type-checks whether or not @types/node is in
// the project's `types`. Metro and web both provide `require` at runtime.
declare function require(name: string): ExpoAvModule;

let expoAv: ExpoAvModule | null | undefined;

function loadExpoAv(): ExpoAvModule | null {
  if (expoAv !== undefined) return expoAv;
  try {
    // Indirect so Metro/webpack cannot resolve it statically and fail the build.
    const name = 'expo-av';
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    expoAv = require(name);
  } catch {
    expoAv = null;
  }
  return expoAv;
}

export function hasNativeAudio(): boolean {
  return loadExpoAv() !== null;
}

const isWeb = typeof window !== 'undefined' && typeof document !== 'undefined';

/* -------------------------------------------------------------------------- */
/* playback                                                                    */
/* -------------------------------------------------------------------------- */

let webAudio: HTMLAudioElement | null = null;
let nativeSound: any = null;

function dataUri(base64: string, mediaType = 'audio/mpeg'): string {
  return `data:${mediaType};base64,${base64}`;
}

/**
 * Play a base64 clip, resolving when it finishes.
 *
 * Returns whether audio was actually heard. It never throws — losing the audio
 * must not stop the call — but the caller needs to know it failed so it can
 * speak the prompt some other way rather than leaving the pilgrim in silence.
 */
export async function playBase64(
  base64: string,
  mediaType = 'audio/mpeg',
  options: { speaker?: boolean } = {}
): Promise<boolean> {
  await stopPlayback();
  if (!base64) return false;

  const uri = dataUri(base64, mediaType);
  const av = loadExpoAv();

  if (av?.Audio) {
    try {
      await av.Audio.setAudioModeAsync({
        allowsRecordingIOS: false,
        playsInSilentModeIOS: true,
        // The one place the speaker toggle has real effect.
        playThroughEarpieceAndroid: options.speaker === false,
      });
      const { sound } = await av.Audio.Sound.createAsync({ uri }, { shouldPlay: true });
      nativeSound = sound;
      await new Promise<void>((resolve) => {
        sound.setOnPlaybackStatusUpdate((status: any) => {
          if (status?.didJustFinish || status?.error) resolve();
        });
      });
      await stopPlayback();
      return true;
    } catch (error) {
      console.warn('[ivr] native playback failed, falling back to web audio', error);
    }
  }

  if (typeof window !== 'undefined' && typeof (window as any).Audio !== 'undefined') {
    try {
      const audio = new (window as any).Audio(uri);
      webAudio = audio;
      let played = true;
      await new Promise<void>((resolve) => {
        audio.onended = () => resolve();
        audio.onerror = () => {
          played = false;
          resolve();
        };
        audio.play().catch(() => {
          played = false;
          resolve();
        });
      });
      return played;
    } catch (error) {
      console.warn('[ivr] audio playback failed', error);
      return false;
    } finally {
      if (webAudio) {
        webAudio = null;
      }
    }
  }
  return false;
}

export async function stopPlayback(): Promise<void> {
  if (nativeSound) {
    try {
      await nativeSound.stopAsync();
      await nativeSound.unloadAsync();
    } catch {
      // Already unloaded.
    }
    nativeSound = null;
  }
  if (webAudio) {
    try {
      webAudio.pause();
      webAudio.currentTime = 0;
    } catch {
      // Detached from the DOM.
    }
    webAudio = null;
  }
}

/* -------------------------------------------------------------------------- */
/* playback queue                                                              */
/* -------------------------------------------------------------------------- */

/**
 * Prompts play one after another, never on top of each other.
 *
 * Turns can arrive faster than they can be spoken — an auto-advanced preset
 * sends two requests before the first prompt has finished, and a slow reply can
 * land while the previous one is still playing. `playBase64` alone would stop
 * the first clip to start the second, so a pilgrim would hear the menu cut off
 * halfway through the option they were waiting for.
 *
 * Recording waits on this queue too: the microphone must not open while the
 * question is still being read out, or the prompt ends up inside the recording.
 */

let tail: Promise<void> = Promise.resolve();
/** Bumped by `clearPlaybackQueue`, so already-queued clips drop instead of playing. */
let generation = 0;
let queued = 0;

/**
 * Queue a clip. Resolves when *this* clip has finished (or was dropped).
 *
 * `text` and `language` are the safety net. If the MP3 will not play — no
 * `expo-av` on this device, a decode failure, a backend with no OpenAI key and
 * so no `audioBase64` at all — the prompt is read out by the device's own
 * voice instead. A menu that sounds robotic is still a menu; a silent one is a
 * dead phone line.
 */
export function enqueuePlayback(
  base64: string | null,
  mediaType = 'audio/mpeg',
  options: { speaker?: boolean; text?: string; language?: Language } = {}
): Promise<void> {
  const mine = generation;
  queued += 1;

  const run = tail.then(async () => {
    try {
      // Superseded while it sat in the queue — the caller has moved on.
      if (mine !== generation) return;

      const heard = base64
        ? await playBase64(base64, mediaType, options)
        : false;

      if (!heard && options.text && mine === generation) {
        await speakOnDevice(options.text, options.language ?? 'en');
      }
    } finally {
      queued -= 1;
    }
  });

  // The chain must survive a failed clip, or one bad prompt wedges the call.
  tail = run.catch(() => {});
  return tail;
}

/** Resolves once nothing is playing or waiting to play. */
export function whenPlaybackIdle(): Promise<void> {
  return tail;
}

export function isPlaybackPending(): boolean {
  return queued > 0;
}

/** Drop anything queued and stop what is playing — audio or device speech. */
export async function clearPlaybackQueue(): Promise<void> {
  generation += 1;
  await stopPlayback();
  // The fallback path speaks through the device, which `stopPlayback` knows
  // nothing about. Without this, pressing a key would leave the old prompt
  // still talking over the new one.
  await stopSpeaking();
}

/* -------------------------------------------------------------------------- */
/* recording (push to talk)                                                    */
/* -------------------------------------------------------------------------- */

let webRecorder: any = null;
let webChunks: BlobPart[] = [];
let nativeRecording: any = null;

export type RecordingHandle = { started: boolean; reason?: string };

export async function startRecording(): Promise<RecordingHandle> {
  // Clear the queue, not just the current clip: a prompt that arrived while the
  // caller was reaching for the button would otherwise start playing into the
  // open microphone and end up in the transcript.
  await clearPlaybackQueue();
  const av = loadExpoAv();

  if (av?.Audio) {
    try {
      const permission = await av.Audio.requestPermissionsAsync();
      if (!permission?.granted) {
        return { started: false, reason: 'Microphone permission was declined.' };
      }
      await av.Audio.setAudioModeAsync({
        allowsRecordingIOS: true,
        playsInSilentModeIOS: true,
      });
      const { recording } = await av.Audio.Recording.createAsync(
        av.Audio.RecordingOptionsPresets?.HIGH_QUALITY
      );
      nativeRecording = recording;
      return { started: true };
    } catch (error) {
      console.warn('[ivr] native recording failed, trying web', error);
    }
  }

  if (!isWeb || !navigator?.mediaDevices?.getUserMedia) {
    return { started: false, reason: 'Recording is not available on this device.' };
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const recorder = new (window as any).MediaRecorder(stream);
    webChunks = [];
    recorder.ondataavailable = (event: any) => {
      if (event.data?.size > 0) webChunks.push(event.data);
    };
    recorder.start(100);
    webRecorder = recorder;
    return { started: true };
  } catch {
    return { started: false, reason: 'Microphone permission was declined.' };
  }
}

/** Stop recording and hand back the clip, or null if nothing usable was caught. */
export async function stopRecording(): Promise<{ blob: Blob; fileName: string } | null> {
  if (nativeRecording) {
    try {
      await nativeRecording.stopAndUnloadAsync();
      const uri = nativeRecording.getURI();
      nativeRecording = null;
      if (!uri) return null;
      const response = await fetch(uri);
      const blob = await response.blob();
      // iOS records .m4a, Android .m4a as well with the HIGH_QUALITY preset.
      return { blob, fileName: 'speech.m4a' };
    } catch (error) {
      console.warn('[ivr] could not finish native recording', error);
      nativeRecording = null;
      return null;
    }
  }

  if (!webRecorder) return null;

  const recorder = webRecorder;
  webRecorder = null;

  const blob = await new Promise<Blob | null>((resolve) => {
    try {
      recorder.onstop = () => {
        resolve(webChunks.length ? new Blob(webChunks, { type: 'audio/webm' }) : null);
      };
      recorder.stop();
      recorder.stream?.getTracks().forEach((track: any) => track.stop());
    } catch {
      resolve(null);
    }
  });
  webChunks = [];

  // Anything this short is a mis-tap, not speech — sending it would spend a
  // transcription call to hear nothing.
  if (!blob || blob.size < 1200) return null;
  return { blob, fileName: 'speech.webm' };
}

export async function cancelRecording(): Promise<void> {
  if (nativeRecording) {
    try {
      await nativeRecording.stopAndUnloadAsync();
    } catch {
      // Already stopped.
    }
    nativeRecording = null;
  }
  if (webRecorder) {
    try {
      webRecorder.stop();
      webRecorder.stream?.getTracks().forEach((track: any) => track.stop());
    } catch {
      // Already stopped.
    }
    webRecorder = null;
  }
  webChunks = [];
}
