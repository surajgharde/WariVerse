import { Feather } from '@expo/vector-icons';
import React, { useState } from 'react';
import { Alert, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import colors from '@/constants/colors';
import { HelplineRow } from '@/components/PhoneBadge';
import { useBottomTabBarHeight } from '@react-navigation/bottom-tabs';
import { useApp } from '@/store/AppContext';

function useSafeTabBarHeight() {
  try {
    return useBottomTabBarHeight();
  } catch {
    return 60;
  }
}

export default function HelpScreen() {
  const { copy, confirmSOS, isLoading, requestLocation } = useApp();
  const insets = useSafeAreaInsets();
  const tabBarHeight = useSafeTabBarHeight();
  const [confirmed, setConfirmed] = useState(false);
  const doSOS = () => Alert.alert(copy.emergency, 'Emergency assistance will be requested and your current location may be shared with the control room.', [{ text: copy.cancel, style: 'cancel' }, { text: copy.confirmSOS, style: 'destructive', onPress: () => { setConfirmed(true); void requestLocation(); void confirmSOS(); } }]);
  return <ScrollView style={styles.screen} contentContainerStyle={[styles.content, { paddingTop: insets.top + 18, paddingBottom: tabBarHeight + 20 }]}><Text style={styles.kicker}>WariVerse</Text><Text style={styles.title}>{copy.helpTitle}</Text><Text style={styles.subtitle}>{copy.helpDescription}</Text><View style={styles.capabilities}>{[['users', copy.crowd], ['trending-up', 'Crowd forecast'], ['navigation', copy.route], ['map-pin', copy.facility], ['home', copy.temple], ['search', 'Lost & Found'], ['user-check', 'Volunteer assistance']].map(([icon, label]) => <View key={label} style={styles.capability}><View style={styles.capabilityIcon}><Feather name={icon as keyof typeof Feather.glyphMap} size={16} color={colors.light.teal} /></View><Text style={styles.capabilityText}>{label}</Text></View>)}</View><View style={styles.emergencyCard}><View style={styles.emergencyTop}><View style={styles.sosIcon}><Feather name="shield" size={21} color={colors.light.white} /></View><View style={styles.emergencyCopy}><Text style={styles.emergencyTitle}>{copy.emergency}</Text><Text style={styles.emergencySubtitle}>{copy.emergencyPrompt}</Text></View></View>{confirmed ? <View style={styles.confirmed}><Feather name="check-circle" size={17} color={colors.light.teal} /><Text style={styles.confirmedText}>{isLoading ? copy.checking : 'Request sent. Help is on the way.'}</Text></View> : <Pressable accessibilityRole="button" onPress={doSOS} style={({ pressed }) => [styles.sosButton, pressed && { opacity: 0.7 }]}><Feather name="phone-call" size={17} color={colors.light.white} /><Text style={styles.sosText}>Call / request emergency help</Text></Pressable>}<Text style={styles.safetyNote}>You’ll be asked to confirm before anything is sent.</Text></View><View style={styles.helplines}><Text style={styles.helplinesHeading}>CALL DIRECTLY</Text><Text style={styles.helplinesNote}>These dial the real emergency services, and work even if WariVerse cannot reach the internet.</Text><HelplineRow /></View><View style={styles.note}><Feather name="info" size={16} color={colors.light.teal} /><Text style={styles.noteText}>For the fastest help, tell WariVerse your gate number or nearby landmark.</Text></View></ScrollView>;
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.light.background },
  content: { paddingHorizontal: 18 },
  kicker: { color: colors.light.mutedForeground, fontFamily: 'Inter_600SemiBold', fontSize: 10, letterSpacing: 1 },
  title: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 29, letterSpacing: -0.8, marginTop: 4 },
  subtitle: { color: colors.light.inkSoft, fontFamily: 'Inter_400Regular', fontSize: 14, lineHeight: 21, marginTop: 9, maxWidth: 330 },
  helplines: { marginTop: 20, gap: 9 },
  helplinesHeading: { color: colors.light.mutedForeground, fontFamily: 'Inter_600SemiBold', fontSize: 10, letterSpacing: 1 },
  helplinesNote: { color: colors.light.inkSoft, fontFamily: 'Inter_400Regular', fontSize: 11, lineHeight: 16 },
  capabilities: { flexDirection: 'row', flexWrap: 'wrap', gap: 9, marginTop: 24 },
  capability: { width: '47%', minHeight: 62, backgroundColor: colors.light.card, borderRadius: 16, borderWidth: 1, borderColor: colors.light.border, padding: 11, flexDirection: 'row', alignItems: 'center', gap: 9 },
  capabilityIcon: { width: 30, height: 30, borderRadius: 10, backgroundColor: colors.light.tealSoft, alignItems: 'center', justifyContent: 'center' },
  capabilityText: { flex: 1, color: colors.light.foreground, fontFamily: 'Inter_600SemiBold', fontSize: 11 },
  emergencyCard: { backgroundColor: '#fae4df', borderRadius: 22, borderWidth: 1, borderColor: '#efc2b8', padding: 16, marginTop: 22 },
  emergencyTop: { flexDirection: 'row', alignItems: 'center', gap: 11 },
  sosIcon: { width: 43, height: 43, borderRadius: 15, backgroundColor: colors.light.destructive, alignItems: 'center', justifyContent: 'center' },
  emergencyCopy: { flex: 1 },
  emergencyTitle: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 16 },
  emergencySubtitle: { color: colors.light.inkSoft, fontFamily: 'Inter_400Regular', fontSize: 12, marginTop: 3 },
  sosButton: { minHeight: 48, backgroundColor: colors.light.destructive, borderRadius: 14, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8, marginTop: 17 },
  sosText: { color: colors.light.white, fontFamily: 'Inter_700Bold', fontSize: 13 },
  safetyNote: { color: colors.light.mutedForeground, fontFamily: 'Inter_400Regular', fontSize: 10, textAlign: 'center', marginTop: 10 },
  confirmed: { backgroundColor: colors.light.white, borderRadius: 14, minHeight: 48, flexDirection: 'row', alignItems: 'center', gap: 8, paddingHorizontal: 13, marginTop: 17 },
  confirmedText: { color: colors.light.teal, fontFamily: 'Inter_600SemiBold', fontSize: 12 },
  note: { flexDirection: 'row', gap: 9, backgroundColor: colors.light.tealSoft, borderRadius: 15, padding: 12, marginTop: 15 },
  noteText: { flex: 1, color: colors.light.teal, fontFamily: 'Inter_500Medium', fontSize: 11, lineHeight: 16 },
});