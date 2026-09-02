import React, { useCallback } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import * as Haptics from 'expo-haptics';
import { ivrTheme } from '@/constants/ivrTheme';

/**
 * The dialpad. Shared by the dialer screen and the in-call DTMF overlay so a
 * key looks and feels identical in both places.
 */

export type KeypadKey = {
  digit: string;
  letters?: string;
};

const KEYS: KeypadKey[][] = [
  [{ digit: '1' }, { digit: '2', letters: 'ABC' }, { digit: '3', letters: 'DEF' }],
  [
    { digit: '4', letters: 'GHI' },
    { digit: '5', letters: 'JKL' },
    { digit: '6', letters: 'MNO' },
  ],
  [
    { digit: '7', letters: 'PQRS' },
    { digit: '8', letters: 'TUV' },
    { digit: '9', letters: 'WXYZ' },
  ],
  [{ digit: '*' }, { digit: '0', letters: '+' }, { digit: '#' }],
];

type Props = {
  onPress: (digit: string) => void;
  /** Keys the backend says are valid right now. Others are dimmed, not hidden —
   *  a keypad that rearranges itself mid-call is disorienting. */
  activeKeys?: string[];
  compact?: boolean;
};

export function IVRKeypad({ onPress, activeKeys, compact = false }: Props) {
  const handle = useCallback(
    (digit: string) => {
      // A dialpad without feedback feels broken; failure here is cosmetic.
      Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
      onPress(digit);
    },
    [onPress]
  );

  return (
    <View style={styles.pad} accessibilityRole="keyboardkey">
      {KEYS.map((row) => (
        <View key={row.map((k) => k.digit).join('')} style={styles.row}>
          {row.map(({ digit, letters }) => {
            const dimmed = activeKeys ? !activeKeys.includes(digit) : false;
            return (
              <Pressable
                key={digit}
                onPress={() => handle(digit)}
                accessibilityRole="button"
                accessibilityLabel={`Key ${digit}`}
                style={({ pressed }) => [
                  styles.key,
                  compact && styles.keyCompact,
                  dimmed && styles.keyDimmed,
                  pressed && styles.keyPressed,
                ]}
              >
                <Text style={[styles.digit, compact && styles.digitCompact]}>{digit}</Text>
                {!compact && letters ? <Text style={styles.letters}>{letters}</Text> : null}
              </Pressable>
            );
          })}
        </View>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  pad: { gap: 14 },
  row: { flexDirection: 'row', justifyContent: 'center', gap: 22 },
  key: {
    width: 74,
    height: 74,
    borderRadius: 37,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: ivrTheme.surface,
    borderWidth: 1,
    borderColor: ivrTheme.border,
  },
  keyCompact: { width: 62, height: 62, borderRadius: 31 },
  keyPressed: {
    backgroundColor: ivrTheme.surfaceRaised,
    borderColor: ivrTheme.teal,
    transform: [{ scale: 0.96 }],
  },
  // Dimmed, never removed: the pad must not reflow between prompts.
  keyDimmed: { opacity: 0.35 },
  digit: {
    fontSize: 28,
    fontFamily: 'Inter_500Medium',
    color: ivrTheme.text,
    lineHeight: 32,
  },
  digitCompact: { fontSize: 24, lineHeight: 28 },
  letters: {
    fontSize: 10,
    letterSpacing: 1.6,
    fontFamily: 'Inter_500Medium',
    color: ivrTheme.textMuted,
    marginTop: 1,
  },
});
