import { Feather } from '@expo/vector-icons';
import React from 'react';
import { Linking, Pressable, StyleSheet, Text, View } from 'react-native';
import colors from '@/constants/colors';
import { HELPLINES } from '@/constants/geo';

/**
 * A phone number as a tappable, high-contrast badge.
 *
 * Phone numbers were previously plain text inside a sentence, which on a phone
 * is the one place a number should never be: someone in trouble should be able
 * to hit it, not read it out to themselves. Urgent numbers get the red
 * treatment so they are findable without reading.
 */

/** `tel:` needs digits and `+` only — spaces and dashes break some dialers. */
function dialable(number: string): string {
  return number.replace(/[^\d+]/g, '');
}

export function PhoneBadge({
  number,
  label,
  detail,
  urgent = false,
}: {
  number: string;
  label?: string;
  detail?: string;
  urgent?: boolean;
}) {
  const call = () => {
    Linking.openURL(`tel:${dialable(number)}`).catch(() => {
      // A tablet with no dialer. Nothing useful to do, and throwing here would
      // take down the card the number is printed on.
    });
  };

  return (
    <Pressable
      onPress={call}
      accessibilityRole="button"
      accessibilityLabel={`Call ${label ? `${label}, ` : ''}${number}`}
      style={({ pressed }) => [
        styles.badge,
        urgent && styles.badgeUrgent,
        pressed && styles.pressed,
      ]}
    >
      <View style={[styles.iconWrap, urgent && styles.iconWrapUrgent]}>
        <Feather
          name="phone-call"
          size={13}
          color={urgent ? colors.light.white : colors.light.teal}
        />
      </View>
      <View style={styles.text}>
        {label ? (
          <Text style={[styles.label, urgent && styles.labelUrgent]} numberOfLines={1}>
            {label}
          </Text>
        ) : null}
        <Text style={[styles.number, urgent && styles.numberUrgent]} numberOfLines={1}>
          {number}
        </Text>
        {detail ? (
          <Text style={styles.detail} numberOfLines={2}>
            {detail}
          </Text>
        ) : null}
      </View>
    </Pressable>
  );
}

/** Every helpline, as a wrapping row of badges. */
export function HelplineRow() {
  return (
    <View style={styles.row}>
      {HELPLINES.map((line) => (
        <PhoneBadge
          key={line.number}
          number={line.number}
          label={line.label}
          detail={line.detail}
          urgent={line.urgent}
        />
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: 9 },
  badge: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 9,
    // Wide enough for "1800-233-1000" plus its label without truncating, and
    // still two-up on a small phone.
    minWidth: 152,
    flexGrow: 1,
    flexBasis: '46%',
    paddingVertical: 10,
    paddingHorizontal: 11,
    borderRadius: 14,
    borderWidth: 1.5,
    borderColor: colors.light.teal,
    backgroundColor: colors.light.tealSoft,
  },
  badgeUrgent: {
    borderColor: colors.light.destructive,
    backgroundColor: '#fef2f2',
  },
  pressed: { opacity: 0.75, transform: [{ scale: 0.98 }] },
  iconWrap: {
    width: 28,
    height: 28,
    borderRadius: 10,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.light.white,
  },
  iconWrapUrgent: { backgroundColor: colors.light.destructive },
  text: { flex: 1 },
  label: {
    color: colors.light.teal,
    fontFamily: 'Inter_700Bold',
    fontSize: 9,
    letterSpacing: 0.7,
    textTransform: 'uppercase',
  },
  labelUrgent: { color: colors.light.destructive },
  number: {
    color: colors.light.foreground,
    fontFamily: 'Inter_700Bold',
    fontSize: 14,
    letterSpacing: 0.3,
  },
  numberUrgent: { color: '#b91c1c' },
  detail: {
    color: colors.light.mutedForeground,
    fontFamily: 'Inter_400Regular',
    fontSize: 10,
    marginTop: 1,
  },
});
