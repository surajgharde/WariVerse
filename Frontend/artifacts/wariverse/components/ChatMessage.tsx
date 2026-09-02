import { Feather } from '@expo/vector-icons';
import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import colors from '@/constants/colors';
import { ToolWidgetRenderer } from '@/components/WidgetCards';
import type { Language, Message } from '@/types/domain';

export function ChatMessage({
  message,
  language,
  locationPermission,
  onSpeak,
  onStopSpeaking,
  onViewMap,
  onViewRoute,
  onConfirmSOS,
  onTalk,
  onRequestLocation,
}: {
  message: Message;
  language: Language;
  locationPermission?: string;
  onSpeak: (text: string) => void;
  onStopSpeaking: () => void;
  onViewMap: () => void;
  onViewRoute: (destLat?: number, destLng?: number, name?: string, phone?: string) => void;
  onConfirmSOS: () => void;
  onTalk: () => void;
  onRequestLocation?: () => void;
}) {
  const isUser = message.role === 'user';
  return (
    <View style={[styles.messageWrap, isUser && styles.userWrap]}>
      <View style={[styles.bubble, isUser ? styles.userBubble : styles.assistantBubble]}>
        <Text style={[styles.messageText, isUser && styles.userText]}>{message.text}</Text>
        {!isUser && message.text && (
          <Pressable accessibilityRole="button" accessibilityLabel="Read assistant message aloud" onPress={() => onSpeak(message.text ?? '')} style={({ pressed }) => [styles.readButton, pressed && { opacity: 0.65 }]}>
            <Feather name="volume-2" size={13} color={colors.light.teal} />
            <Text style={styles.readText}>Read aloud</Text>
          </Pressable>
        )}
        {isUser && message.isVoice && (
          <View style={styles.voiceTag}>
            <Feather name="mic" size={11} color={colors.light.white} />
            <Text style={styles.voiceText}>Voice</Text>
          </View>
        )}
      </View>
      {!isUser && message.widgets?.map((widget, index) => (
        <View key={`${message.id}-widget-${index}`} style={styles.widget}>
          <ToolWidgetRenderer
            widget={widget}
            language={language}
            locationPermission={locationPermission}
            onViewMap={onViewMap}
            onViewRoute={onViewRoute}
            onConfirmSOS={onConfirmSOS}
            onTalk={onTalk}
            onRequestLocation={onRequestLocation}
          />
        </View>
      ))}
      <Text style={[styles.time, isUser && styles.userTime]}>
        {new Date(message.timestamp).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  messageWrap: { alignItems: 'flex-start', marginBottom: 14 },
  userWrap: { alignItems: 'flex-end' },
  bubble: { maxWidth: '89%', borderRadius: 20, paddingHorizontal: 15, paddingVertical: 12 },
  assistantBubble: { backgroundColor: colors.light.card, borderWidth: 1, borderColor: colors.light.border, borderBottomLeftRadius: 6 },
  userBubble: { backgroundColor: colors.light.teal, borderBottomRightRadius: 6 },
  messageText: { color: colors.light.foreground, fontFamily: 'Inter_400Regular', fontSize: 15, lineHeight: 22 },
  userText: { color: colors.light.white },
  readButton: { alignSelf: 'flex-start', flexDirection: 'row', alignItems: 'center', gap: 5, marginTop: 10 },
  readText: { color: colors.light.teal, fontFamily: 'Inter_600SemiBold', fontSize: 11 },
  voiceTag: { flexDirection: 'row', alignItems: 'center', gap: 5, marginTop: 7 },
  voiceText: { color: colors.light.white, fontFamily: 'Inter_500Medium', fontSize: 10 },
  widget: { width: '100%', marginTop: 2 },
  time: { color: colors.light.mutedForeground, fontFamily: 'Inter_400Regular', fontSize: 10, marginTop: 5, marginLeft: 5 },
  userTime: { marginRight: 5 },
});