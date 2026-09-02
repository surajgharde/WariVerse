import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { Feather } from '@expo/vector-icons';
import * as Haptics from 'expo-haptics';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { IVRKeypad } from '@/components/IVRKeypad';
import { ivrTheme } from '@/constants/ivrTheme';
import { ivrApi, newCallSessionId } from '@/services/ivrApi';
import {
  cancelRecording,
  clearPlaybackQueue,
  enqueuePlayback,
  isPlaybackPending,
  startRecording,
  stopRecording,
  whenPlaybackIdle,
} from '@/services/ivrAudio';
import { textToSpeechService } from '@/services/speechService';
import type { IVRCallState, IVRPreset, IVRTurn, Language } from '@/types/domain';

/**
 * The live call. Owns the call lifecycle: dial, connect, play prompts, send
 * DTMF, push to talk, hang up.
 *
 * Prompt text is always rendered, not just spoken. Audio can be unavailable —
 * no speech provider configured, a declined permission, a browser that blocks
 * autoplay — and a caller who can read the menu is still being served.
 */

type Props = {
  preset: IVRPreset;
  language: Language;
  location?: { latitude: number | null; longitude: number | null };
  onEnd: () => void;
};

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function IVRActiveCall({ preset, language, location, onEnd }: Props) {
  const insets = useSafeAreaInsets();
  const [callState, setCallState] = useState<IVRCallState>('dialing');
  const [turn, setTurn] = useState<IVRTurn | null>(null);
  const [seconds, setSeconds] = useState(0);
  const [showKeypad, setShowKeypad] = useState(false);
  const [muted, setMuted] = useState(false);
  const [speaker, setSpeaker] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<string[]>([]);

  const sessionId = useRef(newCallSessionId());
  const mounted = useRef(true);
  // Guards against a prompt from a superseded request overwriting a newer one
  // when the caller presses keys faster than the network replies.
  const turnSeq = useRef(0);
  // Aborts in-flight requests on hang-up, so a retry loop cannot outlive the
  // call and reopen a menu nobody is listening to.
  const hangUpSignal = useRef(new AbortController());
  // Whether the talk button is physically down. Push-to-talk waits for the
  // prompt to finish, and the caller may let go during that wait.
  const holding = useRef(false);
  const recording = useRef(false);

  useEffect(() => {
    mounted.current = true;
    const aborter = hangUpSignal.current;
    return () => {
      mounted.current = false;
      aborter.abort();
      clearPlaybackQueue();
      cancelRecording();
    };
  }, []);

  /** Shown while the transport is retrying, so a stall never looks like a hang. */
  const noticeRetry = useCallback((round: number, total: number) => {
    if (!mounted.current) return;
    setCallState('reconnecting');
    setError(`Weak signal — reconnecting (${round} of ${total})…`);
  }, []);

  const call = useCallback(
    () => ({ onRetry: noticeRetry, signal: hangUpSignal.current.signal }),
    [noticeRetry]
  );

  /** Speak a turn, unless muted. Always shows the text. */
  const present = useCallback(
    async (next: IVRTurn, seq: number) => {
      if (!mounted.current || seq !== turnSeq.current) return;
      setTurn(next);
      setHistory((prior) => [...prior.slice(-8), next.prompt]);
      setError(null);

      if (next.endsSession) {
        setCallState('ended');
        return;
      }
      if (!next.audioBase64 || muted) {
        if (!muted && next.prompt) {
          textToSpeechService.speak(next.prompt, next.language || language);
        }
        setCallState('connected');
        return;
      }

      setCallState('speaking');
      try {
        await enqueuePlayback(next.audioBase64, next.mediaType, {
          speaker,
          text: next.prompt,
          language: next.language,
        });
      } catch (err) {
        console.warn('Base64 playback failed, falling back to TTS:', err);
        if (!muted && next.prompt) {
          await textToSpeechService.speak(next.prompt, next.language || language);
        }
      }
      if (mounted.current && seq === turnSeq.current) setCallState('connected');
    },
    [muted, speaker, language]
  );

  /**
   * Open the session and walk any preset shortcut.
   *
   * Also the reconnect path: `/api/ivr/session/start` re-resolves the same
   * session id and returns the menu, so a call that dropped comes back where a
   * caller would expect rather than in a state the app guessed at.
   */
  const dial = useCallback(async () => {
    setCallState('connecting');
    setError(null);
    const seq = ++turnSeq.current;

    try {
      let current = await ivrApi.start(
        {
          sessionId: sessionId.current,
          language: preset.language ?? language,
          latitude: location?.latitude,
          longitude: location?.longitude,
        },
        call()
      );

      // Walk the preset's shortcut without speaking the steps along the way —
      // the caller asked for the destination, not a tour of the menu. Only the
      // turn they land on is presented.
      for (const key of preset.autoKeys ?? []) {
        if (current.endsSession || seq !== turnSeq.current) break;
        current = await ivrApi.press(
          {
            sessionId: sessionId.current,
            key,
            latitude: location?.latitude,
            longitude: location?.longitude,
          },
          call()
        );
      }

      await present(current, seq);
    } catch (err) {
      if (!mounted.current || seq !== turnSeq.current) return;
      setError(
        err instanceof Error ? err.message : 'Could not connect. Please try again.'
      );
      setCallState('failed');
    }
  }, [call, language, location, present, preset]);

  // Dial once per mounted call. `dial` changes with mute/speaker through
  // `present`, and redialling on those would restart the call mid-sentence.
  useEffect(() => {
    dial();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Duration ticks while the call is up, including through a reconnect.
  useEffect(() => {
    if (['idle', 'dialing', 'connecting', 'ended', 'failed'].includes(callState)) return;
    const timer = setInterval(() => setSeconds((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [callState]);

  const sendKey = useCallback(
    async (key: string) => {
      if (callState === 'ended' || callState === 'failed') return;
      const seq = ++turnSeq.current;
      // Pressing a key is an interruption: the caller has decided, so drop the
      // rest of the prompt rather than making them listen it out.
      await clearPlaybackQueue();
      setCallState('thinking');
      setError(null);
      try {
        const next = await ivrApi.press(
          {
            sessionId: sessionId.current,
            key,
            latitude: location?.latitude,
            longitude: location?.longitude,
          },
          call()
        );
        await present(next, seq);
      } catch (err) {
        if (!mounted.current || seq !== turnSeq.current) return;
        setError(err instanceof Error ? err.message : 'That did not go through.');
        setCallState('connected');
      }
    },
    [call, callState, location, present]
  );

  /**
   * Hold to talk.
   *
   * The microphone waits for the prompt to finish. Opening it early would put
   * the tail of the question into the recording, and the transcript comes back
   * with the menu read into the middle of it. If the caller lets go during that
   * wait, nothing is recorded at all.
   */
  const beginTalking = useCallback(async () => {
    if (callState === 'ended' || callState === 'failed') return;
    holding.current = true;
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium).catch(() => {});

    if (isPlaybackPending()) {
      setCallState('waiting');
      await whenPlaybackIdle();
    }
    if (!holding.current || !mounted.current) return;

    const handle = await startRecording();
    if (!handle.started) {
      setError(handle.reason ?? 'Could not start recording.');
      setCallState('connected');
      return;
    }
    if (!holding.current) {
      // Released while the permission prompt was up.
      await cancelRecording();
      setCallState('connected');
      return;
    }
    recording.current = true;
    setError(null);
    setCallState('listening');
  }, [callState]);

  const finishTalking = useCallback(async () => {
    const wasRecording = recording.current;
    holding.current = false;
    recording.current = false;

    if (!wasRecording) {
      // Let go before the prompt finished — no clip, nothing to send.
      if (mounted.current && callState === 'waiting') setCallState('connected');
      return;
    }

    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
    setCallState('thinking');
    const seq = ++turnSeq.current;

    const clip = await stopRecording();
    if (!clip) {
      setError('I did not catch that. Hold the button while you speak.');
      setCallState('connected');
      return;
    }
    try {
      const next = await ivrApi.speak(
        {
          sessionId: sessionId.current,
          audio: clip.blob,
          fileName: clip.fileName,
        },
        call()
      );
      await present(next, seq);
    } catch (err) {
      if (!mounted.current || seq !== turnSeq.current) return;
      setError(
        err instanceof Error ? err.message : 'Could not send that. Use the keypad instead.'
      );
      setCallState('connected');
    }
  }, [call, callState, present]);

  const hangUp = useCallback(async () => {
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning).catch(() => {});
    holding.current = false;
    recording.current = false;
    // Cancels any retry still in flight, so the call really does stop.
    hangUpSignal.current.abort();
    await clearPlaybackQueue();
    await cancelRecording();
    setCallState('ended');
    onEnd();
  }, [onEnd]);

  /** Re-open the session after a failure, keeping the same call id. */
  const reconnect = useCallback(() => {
    hangUpSignal.current = new AbortController();
    dial();
  }, [dial]);

  const toggleMute = useCallback(() => {
    setMuted((was) => {
      if (!was) clearPlaybackQueue();
      return !was;
    });
  }, []);

  const statusLabel = {
    idle: 'Ready',
    dialing: 'Dialling…',
    connecting: 'Connecting…',
    reconnecting: 'Reconnecting…',
    connected: 'Connected',
    waiting: 'Wait for the prompt…',
    listening: 'Listening…',
    thinking: 'Working…',
    speaking: 'Speaking',
    ended: 'Call ended',
    failed: 'Call failed',
  }[callState];

  // The timer keeps running through a reconnect: from the caller's side the
  // call never dropped, and resetting it would suggest otherwise.
  const live = [
    'connected',
    'speaking',
    'listening',
    'thinking',
    'waiting',
    'reconnecting',
  ].includes(callState);
  const activeKeys = turn?.options.map((o) => o.key);

  return (
    <View style={[styles.screen, { paddingTop: insets.top + 12 }]}>
      {/* header ------------------------------------------------------------ */}
      <View style={styles.header}>
        <Text style={styles.callee}>{preset.label}</Text>
        <Text style={styles.number}>{preset.number}</Text>

        <View style={styles.statusRow}>
          <View
            style={[
              styles.statusDot,
              live && styles.statusDotLive,
              callState === 'failed' && styles.statusDotFailed,
            ]}
          />
          <Text style={styles.status}>{statusLabel}</Text>
          {live ? <Text style={styles.timer}>{formatDuration(seconds)}</Text> : null}
        </View>
      </View>

      {/* the speaking indicator, and what is being said -------------------- */}
      <View style={styles.stage}>
        {!showKeypad ? (
          <View
            style={[
              styles.orb,
              callState === 'speaking' && styles.orbSpeaking,
              callState === 'listening' && styles.orbListening,
            ]}
          >
            {['thinking', 'connecting', 'reconnecting', 'waiting'].includes(callState) ? (
              <ActivityIndicator
                color={callState === 'reconnecting' ? ivrTheme.amber : ivrTheme.teal}
                size="large"
              />
            ) : (
              <Feather
                name={
                  callState === 'listening'
                    ? 'mic'
                    : callState === 'speaking'
                      ? 'volume-2'
                      : 'phone-call'
                }
                size={34}
                color={callState === 'listening' ? ivrTheme.amber : ivrTheme.teal}
              />
            )}
          </View>
        ) : null}

        <ScrollView
          style={[styles.transcript, showKeypad && styles.transcriptCompact]}
          contentContainerStyle={styles.transcriptBody}
          showsVerticalScrollIndicator={true}
        >
          {/* Always readable, never audio-only: the prompt has to survive a
              muted call, a declined mic, or a backend with no TTS key. */}
          <Text style={styles.prompt}>{turn?.prompt ?? 'Connecting to WariVerse…'}</Text>
          {history.length > 1 ? (
            <Text style={styles.history}>{history[history.length - 2]}</Text>
          ) : null}
        </ScrollView>

        {error ? (
          <View style={styles.errorBox}>
            <Feather name="alert-circle" size={14} color={ivrTheme.amber} />
            <Text style={styles.errorText}>{error}</Text>
            {/* A dropped call is recoverable — the session lives on the server,
                so reconnecting returns to the menu rather than starting over. */}
            {callState === 'failed' ? (
              <Pressable
                onPress={reconnect}
                accessibilityRole="button"
                accessibilityLabel="Reconnect"
                style={({ pressed }) => [styles.retry, pressed && styles.retryPressed]}
              >
                <Text style={styles.retryLabel}>Reconnect</Text>
              </Pressable>
            ) : null}
          </View>
        ) : null}
      </View>

      {/* options the backend says are valid -------------------------------- */}
      {turn?.options?.length && !showKeypad ? (
        <View style={styles.options}>
          {turn.options.map((option) => (
            <Pressable
              key={option.key}
              onPress={() => sendKey(option.key)}
              style={({ pressed }) => [styles.option, pressed && styles.optionPressed]}
              accessibilityRole="button"
              accessibilityLabel={`${option.label}, press ${option.key}`}
            >
              <Text style={styles.optionKey}>{option.key}</Text>
              <Text style={styles.optionLabel} numberOfLines={2}>
                {option.label}
              </Text>
            </Pressable>
          ))}
        </View>
      ) : null}

      {showKeypad ? (
        <View style={styles.keypadOverlay}>
          <IVRKeypad onPress={sendKey} activeKeys={activeKeys} compact />
        </View>
      ) : null}

      {/* controls ---------------------------------------------------------- */}
      <View style={[styles.controls, { paddingBottom: insets.bottom + 18 }]}>
        <View style={styles.controlRow}>
          <ControlButton
            icon={muted ? 'volume-x' : 'volume-2'}
            label={muted ? 'Unmute' : 'Mute'}
            active={muted}
            onPress={toggleMute}
          />
          <ControlButton
            icon="grid"
            label="Keypad"
            active={showKeypad}
            onPress={() => setShowKeypad((v) => !v)}
          />
          <ControlButton
            icon="speaker"
            label="Speaker"
            active={speaker}
            onPress={() => setSpeaker((v) => !v)}
          />
        </View>

        {/* Push to talk: held, not toggled, so it cannot be left recording. */}
        <Pressable
          onPressIn={beginTalking}
          onPressOut={finishTalking}
          disabled={!live}
          accessibilityRole="button"
          accessibilityLabel="Hold to speak"
          style={({ pressed }) => [
            styles.talk,
            callState === 'listening' && styles.talkActive,
            (pressed || !live) && styles.talkDim,
          ]}
        >
          <Feather
            name="mic"
            size={20}
            color={callState === 'listening' ? '#fff' : ivrTheme.text}
          />
          <Text style={styles.talkLabel}>
            {callState === 'listening'
              ? 'Release to send'
              : callState === 'waiting'
                ? 'Keep holding…'
                : 'Hold to speak'}
          </Text>
        </Pressable>

        <Pressable
          onPress={hangUp}
          accessibilityRole="button"
          accessibilityLabel="End call"
          style={({ pressed }) => [styles.hangup, pressed && styles.hangupPressed]}
        >
          <Feather name="phone-off" size={26} color="#fff" />
        </Pressable>
      </View>
    </View>
  );
}

function ControlButton({
  icon,
  label,
  active,
  onPress,
}: {
  icon: React.ComponentProps<typeof Feather>['name'];
  label: string;
  active?: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={() => {
        Haptics.selectionAsync().catch(() => {});
        onPress();
      }}
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ selected: !!active }}
      style={({ pressed }) => [
        styles.control,
        active && styles.controlActive,
        pressed && styles.controlPressed,
      ]}
    >
      <Feather name={icon} size={20} color={active ? ivrTheme.teal : ivrTheme.text} />
      <Text style={[styles.controlLabel, active && styles.controlLabelActive]}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: ivrTheme.background, paddingHorizontal: 20 },

  header: { alignItems: 'center', gap: 4 },
  callee: { fontSize: 22, fontFamily: 'Inter_600SemiBold', color: ivrTheme.text },
  number: { fontSize: 14, fontFamily: 'Inter_400Regular', color: ivrTheme.textMuted },
  statusRow: { flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 6 },
  statusDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: ivrTheme.textFaint,
  },
  statusDotLive: { backgroundColor: ivrTheme.green },
  statusDotFailed: { backgroundColor: ivrTheme.red },
  status: { fontSize: 13, fontFamily: 'Inter_500Medium', color: ivrTheme.textMuted },
  timer: {
    fontSize: 13,
    fontFamily: 'Inter_500Medium',
    color: ivrTheme.text,
    // Tabular-ish: stops the row jittering as digits change.
    minWidth: 44,
    textAlign: 'right',
  },

  stage: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 18 },
  orb: {
    width: 108,
    height: 108,
    borderRadius: 54,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: ivrTheme.surface,
    borderWidth: 2,
    borderColor: ivrTheme.border,
  },
  orbSpeaking: {
    borderColor: ivrTheme.teal,
    backgroundColor: ivrTheme.tealGlow,
    shadowColor: ivrTheme.teal,
    shadowOpacity: 0.8,
    shadowRadius: 22,
    shadowOffset: { width: 0, height: 0 },
  },
  orbListening: {
    borderColor: ivrTheme.amber,
    backgroundColor: ivrTheme.amberGlow,
    shadowColor: ivrTheme.amber,
    shadowOpacity: 0.8,
    shadowRadius: 22,
    shadowOffset: { width: 0, height: 0 },
  },

  transcript: { maxHeight: 220, alignSelf: 'stretch' },
  transcriptCompact: { maxHeight: 110 },
  transcriptBody: { gap: 10, paddingHorizontal: 4, paddingVertical: 4 },
  prompt: {
    fontSize: 17,
    lineHeight: 25,
    textAlign: 'center',
    fontFamily: 'Inter_400Regular',
    color: ivrTheme.text,
  },
  history: {
    fontSize: 13,
    lineHeight: 19,
    textAlign: 'center',
    fontFamily: 'Inter_400Regular',
    color: ivrTheme.textFaint,
  },

  errorBox: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    paddingHorizontal: 14,
    paddingVertical: 10,
    borderRadius: 12,
    backgroundColor: ivrTheme.amberGlow,
    borderWidth: 1,
    borderColor: ivrTheme.amber,
  },
  errorText: { flex: 1, fontSize: 13, color: ivrTheme.text, fontFamily: 'Inter_400Regular' },
  retry: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 10,
    backgroundColor: ivrTheme.teal,
  },
  retryPressed: { opacity: 0.8 },
  retryLabel: {
    fontSize: 12,
    fontFamily: 'Inter_600SemiBold',
    color: ivrTheme.background,
  },

  options: { gap: 8, marginBottom: 14 },
  option: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    paddingVertical: 11,
    paddingHorizontal: 14,
    borderRadius: 14,
    backgroundColor: ivrTheme.surface,
    borderWidth: 1,
    borderColor: ivrTheme.border,
  },
  optionPressed: { backgroundColor: ivrTheme.surfaceRaised, borderColor: ivrTheme.teal },
  optionKey: {
    width: 26,
    height: 26,
    borderRadius: 13,
    textAlign: 'center',
    lineHeight: 26,
    fontSize: 13,
    fontFamily: 'Inter_600SemiBold',
    color: ivrTheme.background,
    backgroundColor: ivrTheme.teal,
    overflow: 'hidden',
  },
  optionLabel: {
    flex: 1,
    fontSize: 14,
    fontFamily: 'Inter_500Medium',
    color: ivrTheme.text,
  },

  keypadOverlay: { alignItems: 'center', marginBottom: 16 },

  controls: { gap: 16, alignItems: 'center' },
  controlRow: { flexDirection: 'row', gap: 14 },
  control: {
    width: 84,
    paddingVertical: 12,
    borderRadius: 16,
    alignItems: 'center',
    gap: 6,
    backgroundColor: ivrTheme.surface,
    borderWidth: 1,
    borderColor: ivrTheme.border,
  },
  controlActive: { borderColor: ivrTheme.teal, backgroundColor: ivrTheme.tealGlow },
  controlPressed: { opacity: 0.75 },
  controlLabel: { fontSize: 11, fontFamily: 'Inter_500Medium', color: ivrTheme.textMuted },
  controlLabelActive: { color: ivrTheme.teal },

  talk: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 10,
    alignSelf: 'stretch',
    paddingVertical: 15,
    borderRadius: 18,
    backgroundColor: ivrTheme.surfaceRaised,
    borderWidth: 1,
    borderColor: ivrTheme.border,
  },
  talkActive: {
    backgroundColor: ivrTheme.amber,
    borderColor: ivrTheme.amber,
    shadowColor: ivrTheme.amber,
    shadowOpacity: 0.6,
    shadowRadius: 16,
    shadowOffset: { width: 0, height: 0 },
  },
  talkDim: { opacity: 0.6 },
  talkLabel: { fontSize: 15, fontFamily: 'Inter_600SemiBold', color: ivrTheme.text },

  hangup: {
    width: 68,
    height: 68,
    borderRadius: 34,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: ivrTheme.red,
    shadowColor: ivrTheme.red,
    shadowOpacity: 0.55,
    shadowRadius: 18,
    shadowOffset: { width: 0, height: 4 },
  },
  hangupPressed: { opacity: 0.85, transform: [{ scale: 0.96 }] },
});
