import AsyncStorage from '@react-native-async-storage/async-storage';
import * as Haptics from 'expo-haptics';
import * as Location from 'expo-location';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Platform } from 'react-native';
import { getCopy } from '@/constants/copy';
import { authApi, conversationApi, DEFAULT_SESSION_ID } from '@/services/api';
import { mockConversationApi } from '@/services/mockApi';
import { speechService, textToSpeechService } from '@/services/speechService';
import { LOCATION_TIMEOUT_MS, PANDHARPUR_TEMPLE } from '@/constants/geo';
import type { ConversationResponse, Language, LocationState, Message, User } from '@/types/domain';

type AppContextValue = {
  language: Language;
  setLanguage: (language: Language) => void;
  copy: ReturnType<typeof getCopy>;
  messages: Message[];
  isLoading: boolean;
  isReady: boolean;
  error: string | null;
  readAloud: boolean;
  voiceInput: boolean;
  setReadAloud: (value: boolean) => void;
  setVoiceInput: (value: boolean) => void;
  location: LocationState;
  sendMessage: (text: string, isVoice?: boolean) => Promise<void>;
  confirmSOS: () => Promise<ConversationResponse | null>;
  speak: (text: string, language?: Language) => Promise<void>;
  stopSpeaking: () => Promise<void>;
  startRecording: () => Promise<void>;
  stopRecording: () => Promise<void>;
  cancelRecording: () => Promise<void>;
  isRecording: boolean;
  recordingSeconds: number;
  requestLocation: () => Promise<void>;
  clearConversation: () => Promise<void>;
  isOnboarded: boolean;
  finishOnboarding: (language: Language) => Promise<void>;
  user: User | null;
  loginWithPhone: (phoneNumber: string) => Promise<{ success: boolean; otp: string }>;
  verifyOTP: (phoneNumber: string, otp: string) => Promise<boolean>;
  logout: () => Promise<void>;
};

const AppContext = createContext<AppContextValue | null>(null);
const SETTINGS_KEY = 'wariverse-settings';
const MESSAGES_KEY = 'wariverse-messages';
const USER_KEY = 'wariverse-user';
const sessionId = 'wariverse-session';

function createId(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [language, setLanguageState] = useState<Language>('en');
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isReady, setIsReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [readAloud, setReadAloudState] = useState(true);
  const [voiceInput, setVoiceInputState] = useState(true);
  const [isOnboarded, setIsOnboarded] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [location, setLocation] = useState<LocationState>({ latitude: null, longitude: null, permission: 'unknown' });
  const [isRecording, setIsRecording] = useState(false);
  const [recordingSeconds, setRecordingSeconds] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    Promise.all([
      AsyncStorage.getItem(SETTINGS_KEY),
      AsyncStorage.getItem(MESSAGES_KEY),
      AsyncStorage.getItem(USER_KEY),
    ])
      .then(([settings, savedMessages, savedUser]) => {
        if (settings) {
          const parsed = JSON.parse(settings) as { language?: Language; readAloud?: boolean; voiceInput?: boolean; isOnboarded?: boolean };
          if (parsed.language) setLanguageState(parsed.language);
          if (typeof parsed.readAloud === 'boolean') setReadAloudState(parsed.readAloud);
          if (typeof parsed.voiceInput === 'boolean') setVoiceInputState(parsed.voiceInput);
          if (parsed.isOnboarded) setIsOnboarded(true);
        }
        if (savedMessages) setMessages(JSON.parse(savedMessages) as Message[]);
        if (savedUser) setUser(JSON.parse(savedUser) as User);
        setIsReady(true);
      })
      .catch(() => setIsReady(true));
  }, []);

  useEffect(() => {
    if (!isReady) return;
    AsyncStorage.setItem(MESSAGES_KEY, JSON.stringify(messages)).catch(() => undefined);
  }, [messages, isReady]);

  useEffect(() => {
    if (!isReady) return;
    AsyncStorage.setItem(SETTINGS_KEY, JSON.stringify({ language, readAloud, voiceInput, isOnboarded })).catch(() => undefined);
  }, [language, readAloud, voiceInput, isOnboarded, isReady]);

  useEffect(() => {
    if (!isReady) return;
    if (user) {
      AsyncStorage.setItem(USER_KEY, JSON.stringify(user)).catch(() => undefined);
    } else {
      AsyncStorage.removeItem(USER_KEY).catch(() => undefined);
    }
  }, [user, isReady]);

  useEffect(() => {
    if (isReady) {
      void requestLocation();
    }
  }, [isReady]);

  const setLanguage = useCallback((next: Language) => {
    setLanguageState(next);
    Haptics.selectionAsync().catch(() => undefined);
  }, []);
  const setReadAloud = useCallback((value: boolean) => setReadAloudState(value), []);
  const setVoiceInput = useCallback((value: boolean) => setVoiceInputState(value), []);
  const appCopy = getCopy(language);

  const sendMessage = useCallback(
    async (text: string, isVoice = false) => {
      if (isLoading || !text.trim()) return;
      setError(null);
      const userMessage: Message = { id: createId('user'), role: 'user', text: text.trim(), timestamp: new Date().toISOString(), language, isVoice };
      setMessages((current) => [...current, userMessage]);
      setIsLoading(true);

      // Attempt to acquire fresh real-time GPS location before sending message
      let currentLat = location.latitude;
      let currentLng = location.longitude;
      if (Platform.OS !== 'web') {
        try {
          const perm = await Location.getForegroundPermissionsAsync();
          if (perm.status === 'granted') {
            const currentPos = await Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.Balanced });
            currentLat = currentPos.coords.latitude;
            currentLng = currentPos.coords.longitude;
            setLocation({ latitude: currentLat, longitude: currentLng, permission: 'granted' });
          }
        } catch {
          // Use last known state if position check times out or fails
        }
      } else if (typeof navigator !== 'undefined' && navigator.geolocation && (currentLat === null || currentLng === null)) {
        await new Promise<void>((resolve) => {
          navigator.geolocation.getCurrentPosition(
            (pos) => {
              currentLat = pos.coords.latitude;
              currentLng = pos.coords.longitude;
              setLocation({ latitude: currentLat, longitude: currentLng, permission: 'granted' });
              resolve();
            },
            () => resolve(),
            { timeout: 3000 }
          );
        });
      }

      try {
        let response: ConversationResponse | null = null;
        try {
          response = await conversationApi.sendMessage({
            sessionId,
            language,
            message: text.trim(),
            isVoice,
            latitude: currentLat,
            longitude: currentLng,
          });
        } catch (apiErr) {
          console.warn('Real conversation API failed, falling back to mock:', apiErr);
        }

        if (!response) {
          response = await mockConversationApi.sendMessage({
            sessionId,
            language,
            message: text.trim(),
            latitude: currentLat,
            longitude: currentLng,
          });
        }

        const assistantMessage: Message = { id: response.messageId, role: 'assistant', text: response.responseText, timestamp: new Date().toISOString(), language: response.language, widgets: response.widgets };
        setMessages((current) => [...current, assistantMessage]);
        if (readAloud || isVoice) await textToSpeechService.speak(response.responseText, language);
      } catch {
        setError('Something went wrong. Please try again.');
      } finally {
        setIsLoading(false);
      }
    },
    [isLoading, language, readAloud, location]
  );

  const confirmSOS = useCallback(async () => {
    setIsLoading(true);
    try {
      let response: ConversationResponse;
      try {
        response = await conversationApi.confirmSOS(language, DEFAULT_SESSION_ID);
      } catch {
        // An emergency must still acknowledge on-screen when the network is
        // down — the pilgrim also needs to see the helpline number.
        response = await mockConversationApi.confirmSOS(language);
      }
      setMessages((current) => [...current, { id: response.messageId, role: 'assistant', text: response.responseText, timestamp: new Date().toISOString(), language, widgets: response.widgets }]);
      return response;
    } catch {
      setError('Emergency request could not be completed. Please try again.');
      return null;
    } finally {
      setIsLoading(false);
    }
  }, [language]);

  const speak = useCallback(
    async (text: string, targetLanguage = language) => {
      await textToSpeechService.speak(text, targetLanguage);
    },
    [language]
  );
  const stopSpeaking = useCallback(async () => textToSpeechService.stop(), []);

  const startRecording = useCallback(async () => {
    if (isRecording || !voiceInput) return;
    setError(null);
    await speechService.startRecording(language);
    setIsRecording(true);
    setRecordingSeconds(0);
    timerRef.current = setInterval(() => setRecordingSeconds((seconds) => seconds + 1), 1000);
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => undefined);
  }, [isRecording, voiceInput, language]);

  const stopRecording = useCallback(async () => {
    if (!isRecording) return;
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
    setIsRecording(false);
    const transcript = await speechService.stopRecording(language);
    if (transcript && transcript.trim()) {
      await sendMessage(transcript, true);
    } else {
      setError(
        language === 'mr'
          ? 'आवाज स्पष्ट ऐकू आला नाही. कृपया पुन्हा बोला किंवा टाईप करा.'
          : language === 'hi'
          ? 'आवाज़ साफ़ सुनाई नहीं दी। कृपया फिर से बोलें या टाइप करें।'
          : 'No clear speech detected. Please speak into the mic or type your question.'
      );
    }
  }, [isRecording, language, sendMessage]);

  const cancelRecording = useCallback(async () => {
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
    setIsRecording(false);
    setRecordingSeconds(0);
    await speechService.cancelRecording();
  }, []);

  /**
   * Get a real fix, or fall back to the temple.
   *
   * The map and every "what is near me" query need *some* coordinate to work
   * with. When the phone cannot give one — permission refused, location
   * services off, no fix indoors — falling back to Pandharpur keeps the app
   * useful, and `isFallback` lets the UI say so rather than drawing a "you are
   * here" dot on a place the pilgrim is not.
   */
  const requestLocation = useCallback(async () => {
    const useTemple = (permission: LocationState['permission']) =>
      setLocation({
        latitude: PANDHARPUR_TEMPLE.latitude,
        longitude: PANDHARPUR_TEMPLE.longitude,
        permission,
        isFallback: true,
      });

    if (Platform.OS === 'web') {
      if (typeof navigator === 'undefined' || !navigator.geolocation) {
        // Previously reported `granted` with null coordinates, so the UI showed
        // "You are here" pointing at nowhere.
        useTemple('denied');
        return;
      }
      await new Promise<void>((resolve) => {
        navigator.geolocation.getCurrentPosition(
          (pos) => {
            setLocation({
              latitude: pos.coords.latitude,
              longitude: pos.coords.longitude,
              permission: 'granted',
              isFallback: false,
            });
            resolve();
          },
          () => {
            useTemple('denied');
            resolve();
          },
          {
            enableHighAccuracy: true,
            timeout: LOCATION_TIMEOUT_MS,
            maximumAge: 30_000,
          }
        );
      });
      return;
    }

    try {
      const permission = await Location.requestForegroundPermissionsAsync();
      if (permission.status !== 'granted') {
        useTemple('denied');
        Alert.alert(appCopy.location, appCopy.locationDenied);
        return;
      }

      // A cached fix puts a pin on the map immediately; the high-accuracy read
      // below replaces it a moment later. Without this the map sits on the
      // temple for several seconds even when the phone knows better.
      const known = await Location.getLastKnownPositionAsync({ maxAge: 60_000 });
      if (known) {
        setLocation({
          latitude: known.coords.latitude,
          longitude: known.coords.longitude,
          permission: 'granted',
          isFallback: false,
        });
      }

      const current = await Location.getCurrentPositionAsync({
        accuracy: Location.Accuracy.High,
      });
      setLocation({
        latitude: current.coords.latitude,
        longitude: current.coords.longitude,
        permission: 'granted',
        isFallback: false,
      });
    } catch (error) {
      // Location services switched off, or no fix indoors. This used to reject
      // unhandled and leave permission stuck on 'unknown' forever.
      console.warn('[location] could not get a fix, using Pandharpur', error);
      setLocation((existing) =>
        existing.latitude !== null && !existing.isFallback
          ? existing // A good fix from earlier beats the temple.
          : {
              latitude: PANDHARPUR_TEMPLE.latitude,
              longitude: PANDHARPUR_TEMPLE.longitude,
              permission: existing.permission === 'granted' ? 'granted' : 'denied',
              isFallback: true,
            }
      );
    }
  }, [appCopy.location, appCopy.locationDenied]);

  const clearConversation = useCallback(async () => {
    setMessages([]);
    await AsyncStorage.removeItem(MESSAGES_KEY);
  }, []);

  const finishOnboarding = useCallback(
    async (selectedLanguage: Language) => {
      setLanguageState(selectedLanguage);
      setIsOnboarded(true);
      await AsyncStorage.setItem(SETTINGS_KEY, JSON.stringify({ language: selectedLanguage, readAloud, voiceInput, isOnboarded: true }));
    },
    [readAloud, voiceInput]
  );

  const loginWithPhone = useCallback(async (phoneNumber: string) => {
    try {
      const res = await authApi.requestOTP(phoneNumber);
      return { success: true, otp: res.demoOtp || '123456' };
    } catch {
      return { success: true, otp: '123456' };
    }
  }, []);

  const verifyOTP = useCallback(async (phoneNumber: string, otp: string) => {
    try {
      const res = await authApi.verifyOTP(phoneNumber, otp);
      if (res.success) {
        const newUser: User = {
          id: res.user?.id || createId('usr'),
          phoneNumber: res.user?.phoneNumber || phoneNumber,
          name: res.user?.name || `Warkari (${phoneNumber.slice(-4)})`,
          isAuthenticated: true,
          token: res.token,
          createdAt: new Date().toISOString(),
        };
        setUser(newUser);
        await AsyncStorage.setItem(USER_KEY, JSON.stringify(newUser)).catch(() => undefined);
        return true;
      }
    } catch {
      // Fallback local validation when offline or mock mode
      if (otp === '123456' || otp.length === 6) {
        const newUser: User = {
          id: createId('usr'),
          phoneNumber,
          name: `Warkari (${phoneNumber.slice(-4)})`,
          isAuthenticated: true,
          createdAt: new Date().toISOString(),
        };
        setUser(newUser);
        await AsyncStorage.setItem(USER_KEY, JSON.stringify(newUser)).catch(() => undefined);
        return true;
      }
    }
    return false;
  }, []);

  const logout = useCallback(async () => {
    setUser(null);
    setMessages([]);
    await AsyncStorage.multiRemove([USER_KEY, MESSAGES_KEY]).catch(() => undefined);
  }, []);

  const value = useMemo<AppContextValue>(
    () => ({
      language, setLanguage, copy: appCopy, messages, isLoading, isReady, error, readAloud, voiceInput, setReadAloud, setVoiceInput,
      location, sendMessage, confirmSOS, speak, stopSpeaking, startRecording, stopRecording, cancelRecording, isRecording, recordingSeconds,
      requestLocation, clearConversation, isOnboarded, finishOnboarding, user, loginWithPhone, verifyOTP, logout,
    }),
    [
      language, setLanguage, appCopy, messages, isLoading, isReady, error, readAloud, voiceInput, setReadAloud, setVoiceInput, location,
      sendMessage, confirmSOS, speak, stopSpeaking, startRecording, stopRecording, cancelRecording, isRecording, recordingSeconds,
      requestLocation, clearConversation, isOnboarded, finishOnboarding, user, loginWithPhone, verifyOTP, logout,
    ]
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp() {
  const value = useContext(AppContext);
  if (!value) throw new Error('useApp must be used within AppProvider');
  return value;
}