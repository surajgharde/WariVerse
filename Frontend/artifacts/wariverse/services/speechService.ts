import { getApiBaseUrl } from '@/services/api';
import type { Language } from '@/types/domain';

export interface SpeechService {
  startRecording(language?: Language): Promise<void>;
  stopRecording(language: Language): Promise<string>;
  cancelRecording(): Promise<void>;
}

export interface TextToSpeechService {
  speak(text: string, language: Language): Promise<void>;
  stop(): Promise<void>;
}

const languageTag: Record<Language, string> = { mr: 'mr-IN', hi: 'hi-IN', en: 'en-IN' };

let activeRecognition: any = null;
let activeMediaRecorder: any = null;
let activeAudioChunks: Blob[] = [];
let liveTranscript = '';

export const speechService: SpeechService = {
  async startRecording(language: Language = 'en') {
    liveTranscript = '';
    activeAudioChunks = [];

    if (typeof window !== 'undefined') {
      const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
      if (SpeechRecognition) {
        try {
          const recognition = new SpeechRecognition();
          recognition.continuous = true;
          recognition.interimResults = true;
          recognition.lang = languageTag[language] || 'en-IN';
          recognition.onresult = (event: any) => {
            let current = '';
            for (let i = 0; i < event.results.length; i++) {
              current += event.results[i][0].transcript;
            }
            if (current.trim()) {
              liveTranscript = current.trim();
            }
          };
          recognition.onerror = (err: any) => {
            console.warn('SpeechRecognition error:', err);
          };
          recognition.start();
          activeRecognition = recognition;
        } catch (e) {
          console.warn('Failed to start Web SpeechRecognition:', e);
        }
      }

      if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        try {
          const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
          const recorder = new (window as any).MediaRecorder(stream);
          activeAudioChunks = [];
          recorder.ondataavailable = (event: any) => {
            if (event.data && event.data.size > 0) {
              activeAudioChunks.push(event.data);
            }
          };
          recorder.start(100);
          activeMediaRecorder = recorder;
        } catch (e) {
          console.warn('Failed to start MediaRecorder:', e);
        }
      }
    }
  },

  async stopRecording(language: Language): Promise<string> {
    if (activeRecognition) {
      try {
        activeRecognition.stop();
      } catch {}
      activeRecognition = null;
    }

    let recordedBlob: Blob | null = null;
    if (activeMediaRecorder) {
      await new Promise<void>((resolve) => {
        try {
          activeMediaRecorder.onstop = () => {
            if (activeAudioChunks.length > 0) {
              recordedBlob = new Blob(activeAudioChunks, { type: 'audio/webm' });
            }
            resolve();
          };
          activeMediaRecorder.stop();
          if (activeMediaRecorder.stream) {
            activeMediaRecorder.stream.getTracks().forEach((track: any) => track.stop());
          }
        } catch {
          resolve();
        }
      });
      activeMediaRecorder = null;
    }

    const finalBlob: any = recordedBlob;
    if (finalBlob && finalBlob.size > 100) {
      try {
        const formData = new FormData();
        formData.append('file', finalBlob, 'speech.webm');
        formData.append('language', language);
        const resp = await fetch(`${getApiBaseUrl()}/api/voice/transcribe`, {
          method: 'POST',
          body: formData,
        });
        if (resp.ok) {
          const data = await resp.json();
          if (data.transcript && data.transcript.trim()) {
            return data.transcript.trim();
          }
        }
      } catch (err) {
        console.warn('Backend OpenAI Whisper STT failed, falling back to live transcript:', err);
      }
    }

    if (liveTranscript.trim()) {
      return liveTranscript.trim();
    }

    return '';
  },

  async cancelRecording() {
    if (activeRecognition) {
      try {
        activeRecognition.abort();
      } catch {}
      activeRecognition = null;
    }
    if (activeMediaRecorder) {
      try {
        activeMediaRecorder.stop();
        if (activeMediaRecorder.stream) {
          activeMediaRecorder.stream.getTracks().forEach((track: any) => track.stop());
        }
      } catch {}
      activeMediaRecorder = null;
    }
    activeAudioChunks = [];
    liveTranscript = '';
  },
};

let activeAudioElement: HTMLAudioElement | null = null;

const isWeb = typeof window !== 'undefined' && typeof document !== 'undefined';

/** Speak through the device. Resolves once speaking has finished or failed. */
export async function speakOnDevice(text: string, language: Language): Promise<boolean> {
  const spoken = text.trim();
  if (!spoken) return false;

  try {
    // @ts-ignore
    const Speech = await import('expo-speech');
    await new Promise<void>((resolve) => {
      Speech.speak(spoken, {
        language: languageTag[language] || 'en-IN',
        rate: 0.95,
        onDone: () => resolve(),
        onStopped: () => resolve(),
        onError: () => resolve(),
      });
    });
    return true;
  } catch (error) {
    console.warn('[tts] expo-speech unavailable', error);
  }

  // Last resort for a browser where expo-speech failed to load.
  if (isWeb && 'speechSynthesis' in window) {
    const utterance = new SpeechSynthesisUtterance(spoken);
    utterance.lang = languageTag[language] || 'en-IN';
    window.speechSynthesis.speak(utterance);
    return true;
  }
  return false;
}

/** Silence whatever is currently being said, on any platform. */
export async function stopSpeaking(): Promise<void> {
  if (activeAudioElement) {
    try {
      activeAudioElement.pause();
    } catch {
      // Already detached.
    }
    activeAudioElement = null;
  }
  try {
    // @ts-ignore
    const Speech = await import('expo-speech');
    Speech.stop();
  } catch {
    // Not loadable here; the web branch below still applies.
  }
  if (isWeb && 'speechSynthesis' in window) {
    window.speechSynthesis.cancel();
  }
}

/** The server's MP3 for this text, or null if it could not be played. */
async function playServerAudio(text: string, language: Language): Promise<boolean> {
  if (!isWeb || typeof Audio === 'undefined') return false;

  const url =
    `${getApiBaseUrl()}/api/voice/speak` +
    `?text=${encodeURIComponent(text)}&language=${encodeURIComponent(language)}`;

  try {
    const audio = new Audio(url);
    activeAudioElement = audio;
    await audio.play();
    return true;
  } catch (error) {
    console.warn('[tts] server audio unavailable, using the device voice', error);
    activeAudioElement = null;
    return false;
  }
}

export const textToSpeechService: TextToSpeechService = {
  async speak(text: string, language: Language) {
    const spoken = (text ?? '').trim();
    if (!spoken) return;

    await stopSpeaking();
    if (await playServerAudio(spoken, language)) return;
    await speakOnDevice(spoken, language);
  },

  async stop() {
    await stopSpeaking();
  },
};