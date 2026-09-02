import { Feather } from '@expo/vector-icons';
import { router } from 'expo-router';
import React, { useEffect, useState } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import colors from '@/constants/colors';
import { languages } from '@/constants/copy';
import { BrandMark } from '@/components/BrandMark';
import { useApp } from '@/store/AppContext';
import type { Language } from '@/types/domain';

export default function OnboardingScreen() {
  const { isReady, isOnboarded, finishOnboarding } = useApp();
  const insets = useSafeAreaInsets();
  const [step, setStep] = useState<'welcome' | 'language'>('welcome');
  const [selected, setSelected] = useState<Language>('en');

  useEffect(() => {
    if (isReady && isOnboarded) router.replace('/(tabs)');
  }, [isReady, isOnboarded]);

  if (!isReady || isOnboarded) return <View style={styles.loading}><ActivityIndicator color={colors.light.primary} /></View>;
  if (step === 'language') return <View style={[styles.screen, { paddingTop: insets.top + 22, paddingBottom: insets.bottom + 18 }]}><View style={styles.topRow}><Pressable accessibilityRole="button" onPress={() => setStep('welcome')} style={styles.back}><Feather name="arrow-left" size={20} color={colors.light.foreground} /></Pressable><Text style={styles.step}>01 / 01</Text></View><Text style={styles.title}>Choose how you’d like to talk.</Text><Text style={styles.subTitle}>You can change this anytime in Settings.</Text><View style={styles.languageList}>{languages.map((item) => <Pressable key={item.id} accessibilityRole="radio" accessibilityState={{ selected: selected === item.id }} onPress={() => setSelected(item.id)} style={({ pressed }) => [styles.languageCard, selected === item.id && styles.languageSelected, pressed && styles.pressed]}><View style={styles.languageCopy}><Text style={styles.languageLabel}>{item.label}</Text><Text style={styles.languageNative}>{item.native}</Text><Text style={styles.languageDescription}>{item.description}</Text></View><View style={[styles.radio, selected === item.id && styles.radioSelected]}>{selected === item.id && <View style={styles.radioDot} />}</View></Pressable>)}</View><Pressable accessibilityRole="button" onPress={() => finishOnboarding(selected).then(() => router.replace('/(tabs)'))} style={({ pressed }) => [styles.primaryButton, pressed && styles.pressed]}><Text style={styles.primaryText}>Continue</Text><Feather name="arrow-right" size={18} color={colors.light.white} /></Pressable></View>;
  return <View style={[styles.screen, { paddingTop: insets.top + 22, paddingBottom: insets.bottom + 18 }]}><View style={styles.brandRow}><BrandMark /><Text style={styles.english}>EN</Text></View><View style={styles.welcomeContent}><View style={styles.sun}><View style={styles.sunInner}><Feather name="navigation" size={33} color={colors.light.white} /></View></View><Text style={styles.welcomeTitle}>A calmer way through the Wari.</Text><Text style={styles.welcomeDescription}>Your AI companion for a safer and easier Wari. Ask about crowds, routes, facilities, temple information, and emergency assistance.</Text><View style={styles.trustRow}><View style={styles.trustIcon}><Feather name="check" size={15} color={colors.light.teal} /></View><Text style={styles.trustText}>Simple to ask. Clear to follow.</Text></View><View style={styles.trustRow}><View style={styles.trustIcon}><Feather name="check" size={15} color={colors.light.teal} /></View><Text style={styles.trustText}>Built for every pilgrim.</Text></View></View><View><Pressable accessibilityRole="button" onPress={() => setStep('language')} style={({ pressed }) => [styles.primaryButton, pressed && styles.pressed]}><Text style={styles.primaryText}>Get started</Text><Feather name="arrow-right" size={18} color={colors.light.white} /></Pressable><Pressable accessibilityRole="button" onPress={() => setStep('language')} style={({ pressed }) => [styles.outlineButton, pressed && styles.pressed]}><Text style={styles.outlineText}>Choose language</Text></Pressable></View></View>;
}

const styles = StyleSheet.create({
  loading: { flex: 1, backgroundColor: colors.light.background, alignItems: 'center', justifyContent: 'center' },
  screen: { flex: 1, backgroundColor: colors.light.background, paddingHorizontal: 22, justifyContent: 'space-between' },
  brandRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  english: { color: colors.light.mutedForeground, fontFamily: 'Inter_700Bold', fontSize: 11, letterSpacing: 1 },
  welcomeContent: { alignItems: 'flex-start', marginTop: -20 },
  sun: { width: 92, height: 92, borderRadius: 32, backgroundColor: colors.light.orangeSoft, alignItems: 'center', justifyContent: 'center', transform: [{ rotate: '8deg' }], marginBottom: 28 },
  sunInner: { width: 58, height: 58, borderRadius: 20, backgroundColor: colors.light.primary, alignItems: 'center', justifyContent: 'center', transform: [{ rotate: '-8deg' }] },
  welcomeTitle: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 32, lineHeight: 37, letterSpacing: -1, maxWidth: 330 },
  welcomeDescription: { color: colors.light.inkSoft, fontFamily: 'Inter_400Regular', fontSize: 15, lineHeight: 23, marginTop: 16, maxWidth: 340 },
  trustRow: { flexDirection: 'row', alignItems: 'center', gap: 9, marginTop: 15 },
  trustIcon: { width: 25, height: 25, borderRadius: 9, backgroundColor: colors.light.tealSoft, alignItems: 'center', justifyContent: 'center' },
  trustText: { color: colors.light.teal, fontFamily: 'Inter_600SemiBold', fontSize: 12 },
  primaryButton: { minHeight: 54, backgroundColor: colors.light.primary, borderRadius: 17, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 12, paddingHorizontal: 18 },
  primaryText: { color: colors.light.white, fontFamily: 'Inter_700Bold', fontSize: 15 },
  outlineButton: { minHeight: 52, borderWidth: 1, borderColor: colors.light.border, borderRadius: 17, alignItems: 'center', justifyContent: 'center', marginTop: 10 },
  outlineText: { color: colors.light.teal, fontFamily: 'Inter_700Bold', fontSize: 14 },
  pressed: { opacity: 0.72, transform: [{ scale: 0.985 }] },
  topRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  back: { width: 42, height: 42, borderRadius: 14, backgroundColor: colors.light.card, alignItems: 'center', justifyContent: 'center', borderWidth: 1, borderColor: colors.light.border },
  step: { color: colors.light.mutedForeground, fontFamily: 'Inter_600SemiBold', fontSize: 11, letterSpacing: 1 },
  title: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 29, lineHeight: 35, letterSpacing: -0.8, marginTop: 32 },
  subTitle: { color: colors.light.inkSoft, fontFamily: 'Inter_400Regular', fontSize: 14, marginTop: 10 },
  languageList: { gap: 11, marginTop: 30 },
  languageCard: { minHeight: 92, backgroundColor: colors.light.card, borderWidth: 1, borderColor: colors.light.border, borderRadius: 18, padding: 15, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  languageSelected: { borderColor: colors.light.primary, backgroundColor: colors.light.orangeSoft },
  languageCopy: { flex: 1 },
  languageLabel: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 19 },
  languageNative: { color: colors.light.teal, fontFamily: 'Inter_600SemiBold', fontSize: 12, marginTop: 3 },
  languageDescription: { color: colors.light.mutedForeground, fontFamily: 'Inter_400Regular', fontSize: 11, marginTop: 5 },
  radio: { width: 22, height: 22, borderRadius: 11, borderWidth: 1.5, borderColor: colors.light.input, alignItems: 'center', justifyContent: 'center', marginLeft: 12 },
  radioSelected: { borderColor: colors.light.primary },
  radioDot: { width: 11, height: 11, borderRadius: 6, backgroundColor: colors.light.primary },
});