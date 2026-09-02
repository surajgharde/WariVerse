import React, { useCallback, useMemo, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { Feather } from '@expo/vector-icons';
import * as Haptics from 'expo-haptics';
import { useRouter } from 'expo-router';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { IVRActiveCall } from '@/components/IVRActiveCall';
import { IVRKeypad } from '@/components/IVRKeypad';
import { ivrTheme } from '@/constants/ivrTheme';
import { useApp } from '@/store/AppContext';
import type { IVRPreset } from '@/types/domain';

/**
 * The dialer. Presets first, keypad below, one green button.
 *
 * The numbers are real WariVerse lines, but dialling them here does not place a
 * telephone call — it opens an in-app IVR session against the backend, which is
 * the whole point of the zero-telephony design. The emergency preset is the one
 * exception worth understanding: see `EMERGENCY_NOTE`.
 */

const PRESETS: IVRPreset[] = [
  {
    number: '1800-WARI-HELP',
    label: 'Pilgrimage Assistant',
    description: 'Crowd, darshan, facilities and seva',
  },
  {
    number: '1800-233-1000',
    label: 'Temple Administration',
    description: 'Pandharpur Mandir Samiti',
  },
  {
    number: '112',
    label: 'Emergency SOS',
    description: 'Dispatch and volunteer escalation',
    emergency: true,
    // Straight to the emergency confirmation. Someone in trouble should not
    // have to navigate a menu to reach it — but it still asks before
    // dispatching, exactly as the menu path does.
    autoKeys: ['4'],
  },
];

/**
 * 112 in this dialer opens the IVR emergency flow, which still asks for
 * confirmation before dispatching. It is NOT a PSTN call to the national
 * emergency line. If someone needs the actual 112 operator, the app must place
 * a real call — wire `Linking.openURL('tel:112')` to a separate control before
 * shipping, and do not let this screen be the only path to it.
 */
const EMERGENCY_NOTE = 'Opens the in-app emergency flow — confirmation still required.';

/** Letters on the keypad, so `1800-WARI-HELP` can be typed and matched. */
const LETTER_TO_DIGIT: Record<string, string> = {
  A: '2', B: '2', C: '2',
  D: '3', E: '3', F: '3',
  G: '4', H: '4', I: '4',
  J: '5', K: '5', L: '5',
  M: '6', N: '6', O: '6',
  P: '7', Q: '7', R: '7', S: '7',
  T: '8', U: '8', V: '8',
  W: '9', X: '9', Y: '9', Z: '9',
};

function toDigits(value: string): string {
  return value
    .toUpperCase()
    .split('')
    .map((char) => LETTER_TO_DIGIT[char] ?? char)
    .filter((char) => /[0-9*#]/.test(char))
    .join('');
}

export default function IVRDialerScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const { language, location } = useApp();

  const [entry, setEntry] = useState('');
  const [active, setActive] = useState<IVRPreset | null>(null);

  const matchedPreset = useMemo(() => {
    if (!entry) return null;
    return PRESETS.find((preset) => toDigits(preset.number) === entry) ?? null;
  }, [entry]);

  const appendDigit = useCallback((digit: string) => {
    setEntry((current) => (current.length >= 18 ? current : current + digit));
  }, []);

  const backspace = useCallback(() => {
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
    setEntry((current) => current.slice(0, -1));
  }, []);

  const dial = useCallback(
    (preset?: IVRPreset) => {
      const target =
        preset ??
        matchedPreset ?? {
          number: entry || PRESETS[0].number,
          label: 'WariVerse Assistant',
          description: 'In-app helpline',
        };
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => {});
      setActive(target);
    },
    [entry, matchedPreset]
  );

  if (active) {
    return (
      <IVRActiveCall
        preset={active}
        language={language}
        location={location}
        onEnd={() => setActive(null)}
      />
    );
  }

  return (
    <View style={[styles.screen, { paddingTop: insets.top + 8 }]}>
      <View style={styles.topBar}>
        <Pressable
          onPress={() => router.back()}
          accessibilityRole="button"
          accessibilityLabel="Close dialer"
          style={styles.close}
        >
          <Feather name="chevron-down" size={22} color={ivrTheme.textMuted} />
        </Pressable>
        <Text style={styles.title}>WariVerse Helpline</Text>
        <View style={styles.close} />
      </View>

      <ScrollView
        contentContainerStyle={styles.body}
        showsVerticalScrollIndicator={false}
        keyboardShouldPersistTaps="handled"
      >
        <View style={styles.presets}>
          {PRESETS.map((preset) => (
            <Pressable
              key={preset.number}
              onPress={() => dial(preset)}
              accessibilityRole="button"
              accessibilityLabel={`Call ${preset.label}, ${preset.number}`}
              style={({ pressed }) => [
                styles.preset,
                preset.emergency && styles.presetEmergency,
                pressed && styles.presetPressed,
              ]}
            >
              <View
                style={[
                  styles.presetIcon,
                  preset.emergency && styles.presetIconEmergency,
                ]}
              >
                <Feather
                  name={preset.emergency ? 'alert-triangle' : 'phone'}
                  size={18}
                  color={preset.emergency ? ivrTheme.red : ivrTheme.teal}
                />
              </View>
              <View style={styles.presetText}>
                <Text style={styles.presetLabel}>{preset.label}</Text>
                <Text style={styles.presetNumber}>{preset.number}</Text>
                <Text style={styles.presetDescription} numberOfLines={2}>
                  {preset.emergency ? EMERGENCY_NOTE : preset.description}
                </Text>
              </View>
              <Feather name="chevron-right" size={18} color={ivrTheme.textFaint} />
            </Pressable>
          ))}
        </View>

        <View style={styles.entryRow}>
          <Text
            style={[styles.entry, !entry && styles.entryEmpty]}
            numberOfLines={1}
            accessibilityLabel={entry ? `Dialling ${entry}` : 'No number entered'}
          >
            {entry || 'Enter a number'}
          </Text>
          {entry ? (
            <Pressable
              onPress={backspace}
              onLongPress={() => setEntry('')}
              accessibilityRole="button"
              accessibilityLabel="Backspace"
              style={styles.backspace}
            >
              <Feather name="delete" size={20} color={ivrTheme.textMuted} />
            </Pressable>
          ) : null}
        </View>

        {matchedPreset ? (
          <Text style={styles.matched}>{matchedPreset.label}</Text>
        ) : (
          <View style={styles.matchedSpacer} />
        )}

        <IVRKeypad onPress={appendDigit} />

        <Pressable
          onPress={() => dial()}
          accessibilityRole="button"
          accessibilityLabel="Start call"
          style={({ pressed }) => [styles.call, pressed && styles.callPressed]}
        >
          <Feather name="phone-call" size={26} color="#fff" />
        </Pressable>

        <Text style={styles.footnote}>
          Calls run inside WariVerse over your data connection. No airtime is used.
        </Text>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: ivrTheme.background },
  topBar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 12,
    paddingBottom: 6,
  },
  close: { width: 40, height: 40, alignItems: 'center', justifyContent: 'center' },
  title: { fontSize: 16, fontFamily: 'Inter_600SemiBold', color: ivrTheme.text },

  body: { paddingHorizontal: 20, paddingBottom: 36, alignItems: 'center', gap: 16 },

  presets: { alignSelf: 'stretch', gap: 10 },
  preset: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    padding: 12,
    borderRadius: 16,
    backgroundColor: ivrTheme.surface,
    borderWidth: 1,
    borderColor: ivrTheme.border,
  },
  presetPressed: { backgroundColor: ivrTheme.surfaceRaised, borderColor: ivrTheme.teal },
  presetEmergency: { borderColor: ivrTheme.red },
  presetIcon: {
    width: 40,
    height: 40,
    borderRadius: 20,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: ivrTheme.tealGlow,
  },
  presetIconEmergency: { backgroundColor: ivrTheme.redGlow },
  presetText: { flex: 1, gap: 1 },
  presetLabel: { fontSize: 15, fontFamily: 'Inter_600SemiBold', color: ivrTheme.text },
  presetNumber: {
    fontSize: 13,
    fontFamily: 'Inter_500Medium',
    color: ivrTheme.teal,
    letterSpacing: 0.4,
  },
  presetDescription: {
    fontSize: 11,
    lineHeight: 15,
    fontFamily: 'Inter_400Regular',
    color: ivrTheme.textFaint,
  },

  entryRow: {
    flexDirection: 'row',
    alignItems: 'center',
    alignSelf: 'stretch',
    justifyContent: 'center',
    gap: 10,
    marginTop: 4,
  },
  entry: {
    fontSize: 30,
    letterSpacing: 2,
    fontFamily: 'Inter_500Medium',
    color: ivrTheme.text,
  },
  entryEmpty: { fontSize: 16, letterSpacing: 0, color: ivrTheme.textFaint },
  backspace: { padding: 8 },

  matched: { fontSize: 13, fontFamily: 'Inter_500Medium', color: ivrTheme.teal },
  // Reserves the row so the keypad does not jump when a preset matches.
  matchedSpacer: { height: 18 },

  call: {
    width: 72,
    height: 72,
    borderRadius: 36,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: ivrTheme.green,
    shadowColor: ivrTheme.green,
    shadowOpacity: 0.55,
    shadowRadius: 20,
    shadowOffset: { width: 0, height: 4 },
  },
  callPressed: { opacity: 0.85, transform: [{ scale: 0.96 }] },

  footnote: {
    fontSize: 11,
    lineHeight: 16,
    textAlign: 'center',
    fontFamily: 'Inter_400Regular',
    color: ivrTheme.textFaint,
    paddingHorizontal: 20,
  },
});
