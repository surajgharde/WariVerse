import { Feather } from '@expo/vector-icons';
import { useRouter } from 'expo-router';
import React, { useEffect, useState } from 'react';
import {
  ActivityIndicator,
  BackHandler,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import colors from '@/constants/colors';
import { languages } from '@/constants/copy';
import { useApp } from '@/store/AppContext';
import type { Language } from '@/types/domain';

export default function AuthScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const { loginWithPhone, verifyOTP, finishOnboarding, language, setLanguage, requestLocation } = useApp();

  const [step, setStep] = useState<'phone' | 'otp' | 'setup'>('phone');
  const [phoneNumber, setPhoneNumber] = useState('');
  const [otp, setOtp] = useState('');
  const [selectedLang, setSelectedLang] = useState<Language>(language || 'mr');
  const [name, setName] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [demoBanner, setDemoBanner] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const handleGoBack = () => {
    if (router.canGoBack()) {
      router.back();
    } else {
      router.replace('/(tabs)');
    }
  };

  useEffect(() => {
    const onBackPress = () => {
      handleGoBack();
      return true;
    };
    const subscription = BackHandler.addEventListener('hardwareBackPress', onBackPress);
    return () => subscription.remove();
  }, []);

  const handleSendOTP = async () => {
    if (phoneNumber.length < 10) {
      setErrorMessage('Please enter a valid 10-digit mobile number');
      return;
    }
    setErrorMessage(null);
    setIsLoading(true);
    try {
      const res = await loginWithPhone(phoneNumber);
      if (res.success) {
        setStep('otp');
        setOtp(res.otp); // Pre-fill for seamless demo experience
        setDemoBanner(`Demo OTP sent: ${res.otp}`);
      }
    } catch {
      setErrorMessage('Failed to send OTP. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleVerifyOTP = async () => {
    if (otp.length < 6) {
      setErrorMessage('Please enter the 6-digit OTP code');
      return;
    }
    setErrorMessage(null);
    setIsLoading(true);
    try {
      const success = await verifyOTP(phoneNumber, otp);
      if (success) {
        setStep('setup');
      } else {
        setErrorMessage('Invalid OTP code. Please enter 123456');
      }
    } catch {
      setErrorMessage('Verification failed. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleCompleteSetup = async () => {
    setIsLoading(true);
    try {
      await finishOnboarding(selectedLang);
      await requestLocation();
      handleGoBack();
    } catch {
      setErrorMessage('Setup failed. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <KeyboardAvoidingView
      behavior="padding"
      style={[styles.container, { paddingTop: insets.top + 16, paddingBottom: insets.bottom + 16 }]}
    >
      <View style={styles.header}>
        <Pressable onPress={handleGoBack} style={styles.closeButton}>
          <Feather name="x" size={20} color={colors.light.foreground} />
        </Pressable>
        <Text style={styles.headerTitle}>
          {step === 'setup' ? 'Profile Setup' : 'Account Sign In'}
        </Text>
        <View style={{ width: 36 }} />
      </View>

      <ScrollView contentContainerStyle={styles.content} showsVerticalScrollIndicator={false}>
        <View style={styles.logoBadge}>
          <Feather
            name={step === 'setup' ? 'smile' : step === 'otp' ? 'key' : 'shield'}
            size={32}
            color={colors.light.teal}
          />
        </View>

        <Text style={styles.title}>
          {step === 'phone'
            ? 'Enter Mobile Number'
            : step === 'otp'
            ? 'Verify OTP'
            : 'Welcome to WariVerse!'}
        </Text>
        <Text style={styles.subtitle}>
          {step === 'phone'
            ? 'We will send a 6-digit OTP code to verify your phone number.'
            : step === 'otp'
            ? `Code sent to +91 ${phoneNumber}`
            : 'Choose your language and profile details to customize your Wari journey.'}
        </Text>

        {demoBanner && step !== 'setup' && (
          <View style={styles.banner}>
            <Feather name="info" size={15} color={colors.light.teal} />
            <Text style={styles.bannerText}>{demoBanner}</Text>
          </View>
        )}

        {errorMessage && (
          <View style={styles.errorBanner}>
            <Feather name="alert-circle" size={15} color={colors.light.destructive} />
            <Text style={styles.errorBannerText}>{errorMessage}</Text>
          </View>
        )}

        {step === 'phone' ? (
          <View style={styles.inputWrap}>
            <View style={styles.countryCode}>
              <Text style={styles.countryCodeText}>+91</Text>
            </View>
            <TextInput
              value={phoneNumber}
              onChangeText={(val) => {
                setPhoneNumber(val.replace(/[^0-9]/g, ''));
                setErrorMessage(null);
              }}
              placeholder="10-digit mobile number"
              placeholderTextColor={colors.light.mutedForeground}
              keyboardType="phone-pad"
              maxLength={10}
              style={styles.phoneInput}
              autoFocus
            />
          </View>
        ) : step === 'otp' ? (
          <View style={styles.otpWrap}>
            <TextInput
              value={otp}
              onChangeText={(val) => {
                setOtp(val.replace(/[^0-9]/g, ''));
                setErrorMessage(null);
              }}
              placeholder="123456"
              placeholderTextColor={colors.light.mutedForeground}
              keyboardType="number-pad"
              maxLength={6}
              style={styles.otpInput}
              autoFocus
            />
            <Pressable onPress={() => setStep('phone')} style={styles.changePhone}>
              <Text style={styles.changePhoneText}>Change phone number</Text>
            </Pressable>
          </View>
        ) : (
          <View style={styles.setupWrap}>
            <Text style={styles.sectionLabel}>PREFERRED LANGUAGE / भाषा निवडा</Text>
            <View style={styles.langGrid}>
              {languages.map((item) => (
                <Pressable
                  key={item.id}
                  onPress={() => setSelectedLang(item.id)}
                  style={[
                    styles.langCard,
                    selectedLang === item.id && styles.langCardActive,
                  ]}
                >
                  <Text
                    style={[
                      styles.langLabel,
                      selectedLang === item.id && styles.langLabelActive,
                    ]}
                  >
                    {item.label}
                  </Text>
                  <Text
                    style={[
                      styles.langDesc,
                      selectedLang === item.id && styles.langDescActive,
                    ]}
                  >
                    {item.native}
                  </Text>
                  {selectedLang === item.id && (
                    <View style={styles.checkBadge}>
                      <Feather name="check" size={14} color={colors.light.white} />
                    </View>
                  )}
                </Pressable>
              ))}
            </View>

            <Text style={[styles.sectionLabel, { marginTop: 18 }]}>YOUR NAME (OPTIONAL)</Text>
            <View style={styles.inputWrap}>
              <Feather name="user" size={18} color={colors.light.mutedForeground} style={{ marginRight: 10 }} />
              <TextInput
                value={name}
                onChangeText={setName}
                placeholder="e.g. Rahul Warkari"
                placeholderTextColor={colors.light.mutedForeground}
                style={styles.phoneInput}
              />
            </View>
          </View>
        )}

        <Pressable
          onPress={
            step === 'phone'
              ? handleSendOTP
              : step === 'otp'
              ? handleVerifyOTP
              : handleCompleteSetup
          }
          disabled={isLoading}
          style={({ pressed }) => [
            styles.submitButton,
            isLoading && styles.buttonDisabled,
            pressed && { opacity: 0.8 },
          ]}
        >
          {isLoading ? (
            <ActivityIndicator color={colors.light.white} />
          ) : (
            <Text style={styles.submitText}>
              {step === 'phone'
                ? 'Get OTP'
                : step === 'otp'
                ? 'Verify OTP'
                : 'Complete & Start WariVerse'}
            </Text>
          )}
        </Pressable>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.light.background,
    paddingHorizontal: 20,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: 20,
  },
  closeButton: {
    width: 36,
    height: 36,
    borderRadius: 18,
    backgroundColor: colors.light.card,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 1,
    borderColor: colors.light.border,
  },
  headerTitle: {
    fontFamily: 'Inter_600SemiBold',
    fontSize: 16,
    color: colors.light.foreground,
  },
  content: {
    alignItems: 'center',
    paddingBottom: 30,
  },
  logoBadge: {
    width: 68,
    height: 68,
    borderRadius: 24,
    backgroundColor: colors.light.tealSoft,
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: 16,
  },
  title: {
    fontFamily: 'Inter_700Bold',
    fontSize: 22,
    color: colors.light.foreground,
    textAlign: 'center',
  },
  subtitle: {
    fontFamily: 'Inter_400Regular',
    fontSize: 14,
    color: colors.light.mutedForeground,
    textAlign: 'center',
    marginTop: 6,
    marginBottom: 20,
    paddingHorizontal: 10,
  },
  banner: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    backgroundColor: colors.light.tealSoft,
    paddingHorizontal: 14,
    paddingVertical: 10,
    borderRadius: 12,
    marginBottom: 16,
    width: '100%',
  },
  bannerText: {
    color: colors.light.teal,
    fontFamily: 'Inter_600SemiBold',
    fontSize: 12,
  },
  errorBanner: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    backgroundColor: '#fae4df',
    paddingHorizontal: 14,
    paddingVertical: 10,
    borderRadius: 12,
    marginBottom: 16,
    width: '100%',
  },
  errorBannerText: {
    color: colors.light.destructive,
    fontFamily: 'Inter_500Medium',
    fontSize: 12,
  },
  inputWrap: {
    flexDirection: 'row',
    alignItems: 'center',
    width: '100%',
    height: 54,
    borderRadius: 16,
    backgroundColor: colors.light.card,
    borderWidth: 1,
    borderColor: colors.light.border,
    paddingHorizontal: 14,
    marginBottom: 20,
  },
  countryCode: {
    paddingRight: 12,
    borderRightWidth: 1,
    borderRightColor: colors.light.border,
    marginRight: 12,
  },
  countryCodeText: {
    fontFamily: 'Inter_700Bold',
    fontSize: 15,
    color: colors.light.foreground,
  },
  phoneInput: {
    flex: 1,
    fontSize: 16,
    fontFamily: 'Inter_600SemiBold',
    color: colors.light.foreground,
    ...(Platform.OS === 'web' ? ({ outlineStyle: 'none' } as any) : {}),
  },
  otpWrap: {
    width: '100%',
    alignItems: 'center',
    marginBottom: 20,
  },
  otpInput: {
    width: '100%',
    height: 56,
    borderRadius: 16,
    backgroundColor: colors.light.card,
    borderWidth: 1,
    borderColor: colors.light.border,
    textAlign: 'center',
    fontSize: 24,
    letterSpacing: 8,
    fontFamily: 'Inter_700Bold',
    color: colors.light.foreground,
    ...(Platform.OS === 'web' ? ({ outlineStyle: 'none' } as any) : {}),
  },
  changePhone: {
    marginTop: 12,
  },
  changePhoneText: {
    color: colors.light.teal,
    fontFamily: 'Inter_600SemiBold',
    fontSize: 12,
  },
  setupWrap: {
    width: '100%',
    marginBottom: 20,
  },
  sectionLabel: {
    fontFamily: 'Inter_700Bold',
    fontSize: 11,
    letterSpacing: 0.8,
    color: colors.light.mutedForeground,
    marginBottom: 10,
  },
  langGrid: {
    gap: 10,
  },
  langCard: {
    backgroundColor: colors.light.card,
    borderWidth: 1,
    borderColor: colors.light.border,
    borderRadius: 16,
    padding: 14,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  langCardActive: {
    backgroundColor: colors.light.tealSoft,
    borderColor: colors.light.teal,
  },
  langLabel: {
    fontFamily: 'Inter_700Bold',
    fontSize: 15,
    color: colors.light.foreground,
  },
  langLabelActive: {
    color: colors.light.teal,
  },
  langDesc: {
    fontFamily: 'Inter_400Regular',
    fontSize: 12,
    color: colors.light.mutedForeground,
  },
  langDescActive: {
    color: colors.light.teal,
  },
  checkBadge: {
    width: 22,
    height: 22,
    borderRadius: 11,
    backgroundColor: colors.light.teal,
    alignItems: 'center',
    justifyContent: 'center',
  },
  submitButton: {
    width: '100%',
    height: 52,
    borderRadius: 16,
    backgroundColor: colors.light.primary,
    alignItems: 'center',
    justifyContent: 'center',
    marginTop: 6,
  },
  buttonDisabled: {
    opacity: 0.6,
  },
  submitText: {
    color: colors.light.white,
    fontFamily: 'Inter_700Bold',
    fontSize: 15,
  },
});
