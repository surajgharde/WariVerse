import { Feather } from '@expo/vector-icons';
import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import colors from '@/constants/colors';

export function BrandMark({ compact = false }: { compact?: boolean }) {
  return (
    <View style={styles.row}>
      <View style={[styles.mark, compact && styles.markCompact]}>
        <Feather name="navigation" size={compact ? 15 : 20} color={colors.light.white} />
      </View>
      <View>
        <Text style={[styles.name, compact && styles.nameCompact]}>WariVerse</Text>
        {!compact && <Text style={styles.tagline}>your journey, made easier</Text>}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  mark: { width: 42, height: 42, borderRadius: 14, backgroundColor: colors.light.primary, alignItems: 'center', justifyContent: 'center', transform: [{ rotate: '-10deg' }] },
  markCompact: { width: 32, height: 32, borderRadius: 11 },
  name: { color: colors.light.foreground, fontSize: 19, fontFamily: 'Inter_700Bold', letterSpacing: -0.4 },
  nameCompact: { fontSize: 16 },
  tagline: { color: colors.light.mutedForeground, fontSize: 10, fontFamily: 'Inter_500Medium', marginTop: 1 },
});