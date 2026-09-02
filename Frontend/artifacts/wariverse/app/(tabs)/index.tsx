import { Feather } from '@expo/vector-icons';
import { KeyboardAvoidingView } from 'react-native-keyboard-controller';
import { router, useRouter } from 'expo-router';
import React, { useMemo, useState } from 'react';
import { ActivityIndicator, FlatList, Platform, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import colors from '@/constants/colors';
import { suggestionKeys } from '@/constants/copy';
import { BrandMark } from '@/components/BrandMark';
import { ChatMessage } from '@/components/ChatMessage';
import { useBottomTabBarHeight } from '@react-navigation/bottom-tabs';
import { useApp } from '@/store/AppContext';
import type { Message } from '@/types/domain';

function useSafeTabBarHeight() {
  try {
    return useBottomTabBarHeight();
  } catch {
    return 60;
  }
}

export default function ChatScreen() {
  const { copy, language, messages, isLoading, isReady, error, sendMessage, speak, stopSpeaking, startRecording, stopRecording, cancelRecording, isRecording, recordingSeconds, confirmSOS, user, location, requestLocation } = useApp();
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const tabBarHeight = useSafeTabBarHeight();
  const [draft, setDraft] = useState('');
  const suggestions = useMemo(() => suggestionKeys.map((key) => ({ key, label: copy[key] })), [copy]);
  const submit = (text = draft) => { if (!isLoading && text.trim()) { setDraft(''); void sendMessage(text); } };
  const chatData = useMemo<Message[]>(() => messages, [messages]);

  if (!isReady) return <View style={styles.loading}><ActivityIndicator color={colors.light.primary} /></View>;
  return (
    <KeyboardAvoidingView behavior="padding" keyboardVerticalOffset={0} style={styles.screen}>
      <View style={[styles.header, { paddingTop: insets.top + 12 }]}>
        <BrandMark compact />
        <View style={styles.headerActions}>
          <Pressable accessibilityRole="button" accessibilityLabel="Account" onPress={() => router.push(user ? '/(tabs)/settings' : '/auth')} style={styles.headerButton}>
            <Feather name={user ? 'user-check' : 'user'} size={18} color={user ? colors.light.teal : colors.light.foreground} />
          </Pressable>
          <View style={styles.languagePill}>
            <View style={styles.languageDot} />
            <Text style={styles.languageText}>{language === 'mr' ? 'मराठी' : language === 'hi' ? 'हिंदी' : 'English'}</Text>
          </View>
          <Pressable accessibilityRole="button" accessibilityLabel="Open settings" onPress={() => router.push('/(tabs)/settings')} style={styles.headerButton}>
            <Feather name="settings" size={18} color={colors.light.foreground} />
          </Pressable>
        </View>
      </View>

      {location.permission !== 'granted' && (
        <View style={styles.gpsNoticeBar}>
          <Feather name="map-pin" size={15} color="#92400e" />
          <Text style={styles.gpsNoticeText}>GPS is disconnected. Enable GPS for live nearby facilities.</Text>
          <Pressable onPress={() => void requestLocation()} style={styles.connectGpsHeaderBtn}>
            <Feather name="crosshair" size={13} color={colors.light.white} />
            <Text style={styles.connectGpsHeaderText}>Connect GPS</Text>
          </Pressable>
        </View>
      )}

      {!isReady ? null : (
        <FlatList
          data={chatData}
          keyExtractor={(item) => item.id}
          renderItem={({ item }) => (
            <ChatMessage
              message={item}
              language={language}
              locationPermission={location.permission}
              onSpeak={(text) => void speak(text)}
              onStopSpeaking={() => void stopSpeaking()}
              onViewMap={() => router.push('/(tabs)/map?focus=crowd')}
              onViewRoute={(destLat, destLng, name, phone) => {
                if (destLat && destLng) {
                  router.push(`/(tabs)/map?focus=route&destLat=${destLat}&destLng=${destLng}&destName=${encodeURIComponent(name || 'Destination')}&phone=${encodeURIComponent(phone || '')}`);
                } else {
                  router.push('/(tabs)/map?focus=route');
                }
              }}
              onConfirmSOS={() => void confirmSOS()}
              onTalk={() => submit(copy.help)}
              onRequestLocation={() => void requestLocation()}
            />
          )}
          ListEmptyComponent={
            <View style={styles.empty}>
              <Text style={styles.emptyTitle}>{copy.greeting}</Text>
              <Text style={styles.emptyDescription}>{copy.greetingSub} {copy.noMessages}</Text>
              <View style={styles.suggestionWrap}>
                {suggestions.map(({ key, label }) => (
                  <Pressable key={key} accessibilityRole="button" onPress={() => submit(label)} style={({ pressed }) => [styles.chip, pressed && styles.pressed]}>
                    <Feather name={key === 'crowd' ? 'users' : key === 'facility' ? 'map-pin' : key === 'route' ? 'navigation' : key === 'temple' ? 'home' : 'life-buoy'} size={14} color={colors.light.teal} />
                    <Text style={styles.chipText}>{label}</Text>
                  </Pressable>
                ))}
              </View>
            </View>
          }
          ListHeaderComponent={messages.length > 0 ? <View style={styles.historyIntro}><Text style={styles.historyText}>WariVerse · {copy.recent}</Text></View> : null}
          ListFooterComponent={isLoading ? <View style={styles.typing}><View style={styles.typingDot} /><View style={styles.typingDot} /><View style={styles.typingDot} /><Text style={styles.typingText}>{copy.checking}</Text></View> : null}
          contentContainerStyle={[styles.listContent, { paddingBottom: tabBarHeight + 140 }]}
          keyboardShouldPersistTaps="handled"
          keyboardDismissMode="interactive"
          scrollEnabled={chatData.length > 0}
          showsVerticalScrollIndicator={false}
        />
      )}

      {error && (
        <View style={styles.errorBar}>
          <Feather name="wifi-off" size={14} color={colors.light.destructive} />
          <Text style={styles.errorText}>{error}</Text>
          <Pressable onPress={() => setDraft('')}><Text style={styles.retryText}>Dismiss</Text></Pressable>
        </View>
      )}

      {isRecording && (
        <View style={styles.recordingBar}>
          <View style={styles.recordingPulse}><Feather name="mic" size={16} color={colors.light.white} /></View>
          <View style={styles.recordingInfo}>
            <Text style={styles.recordingTitle}>{copy.listening}</Text>
            <Text style={styles.recordingTime}>0:{String(recordingSeconds).padStart(2, '0')}</Text>
          </View>
          <Pressable accessibilityRole="button" onPress={() => void cancelRecording()} style={styles.cancelRecord}><Text style={styles.cancelRecordText}>{copy.cancel}</Text></Pressable>
          <Pressable accessibilityRole="button" onPress={() => void stopRecording()} style={styles.doneRecord}><Feather name="check" size={17} color={colors.light.white} /></Pressable>
        </View>
      )}

      <View style={[styles.composerWrap, { paddingBottom: tabBarHeight + (Platform.OS === 'web' ? 14 : 8) }]}>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.suggestionRow}>
          {suggestions.map(({ key, label }) => (
            <Pressable key={`quick-${key}`} accessibilityRole="button" onPress={() => submit(label)} style={({ pressed }) => [styles.quickChip, pressed && styles.pressed]}>
              <Feather name={key === 'crowd' ? 'users' : key === 'facility' ? 'map-pin' : key === 'route' ? 'navigation' : key === 'temple' ? 'home' : 'life-buoy'} size={13} color={colors.light.teal} />
              <Text style={styles.quickChipText}>{label}</Text>
            </Pressable>
          ))}
        </ScrollView>
        <View style={styles.composerLabelRow}>
          <Feather name="edit-3" size={14} color={colors.light.teal} />
          <Text style={styles.composerLabel}>Ask WariVerse anything</Text>
        </View>
        <View style={styles.composer}>
          <TextInput
            testID="chat-message-input"
            accessibilityLabel="Type your question"
            value={draft}
            onChangeText={setDraft}
            placeholder={isRecording ? copy.listening : copy.placeholder}
            placeholderTextColor={colors.light.mutedForeground}
            style={styles.input}
            multiline
            maxLength={500}
            editable={!isLoading && !isRecording}
            onSubmitEditing={() => submit()}
          />
          <Pressable accessibilityRole="button" accessibilityLabel={isRecording ? 'Stop recording' : 'Start voice input'} onPress={() => void (isRecording ? stopRecording() : startRecording())} style={({ pressed }) => [styles.micButton, isRecording && styles.micRecording, pressed && styles.pressed]}>
            <Feather name={isRecording ? 'square' : 'mic'} size={18} color={isRecording ? colors.light.white : colors.light.teal} />
          </Pressable>
          <Pressable testID="chat-send-button" accessibilityRole="button" accessibilityLabel={copy.send} onPress={() => submit()} disabled={isLoading || !draft.trim()} style={({ pressed }) => [styles.sendButton, (!draft.trim() || isLoading) && styles.sendDisabled, pressed && styles.pressed]}>
            <Feather name="arrow-up" size={19} color={colors.light.white} />
          </Pressable>
        </View>
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.light.background },
  loading: { flex: 1, backgroundColor: colors.light.background, alignItems: 'center', justifyContent: 'center' },
  header: { paddingHorizontal: 18, paddingBottom: 12, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  headerActions: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  languagePill: { flexDirection: 'row', alignItems: 'center', gap: 6, backgroundColor: colors.light.tealSoft, borderRadius: 15, paddingHorizontal: 9, paddingVertical: 7 },
  languageDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: colors.light.teal },
  languageText: { color: colors.light.teal, fontFamily: 'Inter_600SemiBold', fontSize: 10 },
  headerButton: { width: 34, height: 34, borderRadius: 12, backgroundColor: colors.light.card, borderWidth: 1, borderColor: colors.light.border, alignItems: 'center', justifyContent: 'center' },
  gpsNoticeBar: { flexDirection: 'row', alignItems: 'center', gap: 8, marginHorizontal: 16, marginBottom: 8, paddingHorizontal: 12, paddingVertical: 8, borderRadius: 12, backgroundColor: '#fef3c7', borderWidth: 1, borderColor: '#fde68a' },
  gpsNoticeText: { flex: 1, color: '#92400e', fontFamily: 'Inter_500Medium', fontSize: 11 },
  connectGpsHeaderBtn: { flexDirection: 'row', alignItems: 'center', gap: 5, backgroundColor: colors.light.primary, paddingHorizontal: 10, paddingVertical: 6, borderRadius: 9 },
  connectGpsHeaderText: { color: colors.light.white, fontFamily: 'Inter_600SemiBold', fontSize: 11 },
  listContent: { paddingHorizontal: 18, flexGrow: 1 },
  historyIntro: { paddingBottom: 14, paddingTop: 2 },
  historyText: { color: colors.light.mutedForeground, fontFamily: 'Inter_600SemiBold', fontSize: 10, letterSpacing: 1 },
  empty: { flex: 1, justifyContent: 'center', paddingBottom: 16 },
  emptyIcon: { width: 51, height: 51, backgroundColor: colors.light.orangeSoft, borderRadius: 18, alignItems: 'center', justifyContent: 'center', marginBottom: 19 },
  emptyTitle: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 24, lineHeight: 30, letterSpacing: -0.5 },
  emptyDescription: { color: colors.light.inkSoft, fontFamily: 'Inter_400Regular', fontSize: 14, lineHeight: 21, marginTop: 9, maxWidth: 320 },
  suggestionWrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginTop: 25 },
  chip: { flexDirection: 'row', alignItems: 'center', gap: 6, borderRadius: 14, backgroundColor: colors.light.card, borderWidth: 1, borderColor: colors.light.border, paddingHorizontal: 11, paddingVertical: 9 },
  chipText: { color: colors.light.teal, fontFamily: 'Inter_600SemiBold', fontSize: 11 },
  typing: { flexDirection: 'row', alignItems: 'center', gap: 5, paddingVertical: 10 },
  typingDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: colors.light.primary },
  typingText: { color: colors.light.mutedForeground, fontFamily: 'Inter_500Medium', fontSize: 11, marginLeft: 3 },
  suggestionRow: { flexDirection: 'row', gap: 6, paddingBottom: 8, paddingHorizontal: 2 },
  quickChip: { flexDirection: 'row', alignItems: 'center', gap: 5, backgroundColor: colors.light.card, borderWidth: 1, borderColor: colors.light.border, borderRadius: 12, paddingHorizontal: 11, paddingVertical: 6 },
  quickChipText: { color: colors.light.teal, fontFamily: 'Inter_600SemiBold', fontSize: 11 },
  composerWrap: { paddingHorizontal: 16, paddingTop: 8, backgroundColor: colors.light.background },
  composerLabelRow: { flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 7, paddingHorizontal: 4 },
  composerLabel: { color: colors.light.teal, fontFamily: 'Inter_700Bold', fontSize: 12 },
  composer: { minHeight: 56, maxHeight: 120, borderRadius: 20, backgroundColor: colors.light.card, borderWidth: 1, borderColor: colors.light.border, flexDirection: 'row', alignItems: 'flex-end', padding: 7, gap: 6 },
  input: { flex: 1, minHeight: 40, color: colors.light.foreground, fontFamily: 'Inter_400Regular', fontSize: 14, lineHeight: 19, paddingHorizontal: 10, paddingVertical: 10, maxHeight: 100, ...(Platform.OS === 'web' ? ({ outlineStyle: 'none', outlineWidth: 0, outlineColor: 'transparent' } as any) : {}) },
  micButton: { width: 40, height: 40, borderRadius: 14, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.light.tealSoft },
  micRecording: { backgroundColor: colors.light.primary },
  sendButton: { width: 40, height: 40, borderRadius: 14, backgroundColor: colors.light.primary, alignItems: 'center', justifyContent: 'center' },
  sendDisabled: { backgroundColor: colors.light.input },
  pressed: { opacity: 0.7, transform: [{ scale: 0.97 }] },
  errorBar: { flexDirection: 'row', alignItems: 'center', gap: 7, marginHorizontal: 16, padding: 10, borderRadius: 12, backgroundColor: '#fae4df' },
  errorText: { flex: 1, color: colors.light.destructive, fontFamily: 'Inter_500Medium', fontSize: 11 },
  retryText: { color: colors.light.destructive, fontFamily: 'Inter_700Bold', fontSize: 11 },
  recordingBar: { marginHorizontal: 16, marginBottom: 5, minHeight: 61, backgroundColor: colors.light.teal, borderRadius: 18, padding: 10, flexDirection: 'row', alignItems: 'center', gap: 10 },
  recordingPulse: { width: 37, height: 37, borderRadius: 13, backgroundColor: colors.light.primary, alignItems: 'center', justifyContent: 'center' },
  recordingInfo: { flex: 1 },
  recordingTitle: { color: colors.light.white, fontFamily: 'Inter_600SemiBold', fontSize: 12 },
  recordingTime: { color: '#cfe4db', fontFamily: 'Inter_500Medium', fontSize: 11, marginTop: 2 },
  cancelRecord: { paddingHorizontal: 8, paddingVertical: 8 },
  cancelRecordText: { color: colors.light.white, fontFamily: 'Inter_600SemiBold', fontSize: 11 },
  doneRecord: { width: 35, height: 35, borderRadius: 12, backgroundColor: colors.light.primary, alignItems: 'center', justifyContent: 'center' },
});