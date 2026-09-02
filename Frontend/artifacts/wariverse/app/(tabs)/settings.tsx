import { Feather } from '@expo/vector-icons';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { useRouter } from 'expo-router';
import React, { useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
} from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import colors from '@/constants/colors';
import { languages } from '@/constants/copy';
import { useBottomTabBarHeight } from '@react-navigation/bottom-tabs';
import { communityApi, type CommunityServiceItem } from '@/services/api';
import { useApp } from '@/store/AppContext';

const MY_SEVAS_KEY = 'wariverse-my-sevas';

function useSafeTabBarHeight() {
  try {
    return useBottomTabBarHeight();
  } catch {
    return 60;
  }
}

type SevaCategory = 'food' | 'water' | 'medical' | 'rest' | 'accommodation';

const SEVA_CATEGORIES: { id: SevaCategory; label: string; icon: keyof typeof Feather.glyphMap }[] = [
  { id: 'food', label: 'Food (Annachhatra)', icon: 'shopping-bag' },
  { id: 'water', label: 'Drinking Water', icon: 'droplet' },
  { id: 'medical', label: 'Medical Aid', icon: 'heart' },
  { id: 'rest', label: 'Rest Shelter', icon: 'coffee' },
  { id: 'accommodation', label: 'Night Stay', icon: 'home' },
];

export default function SettingsScreen() {
  const router = useRouter();
  const {
    copy,
    language,
    setLanguage,
    readAloud,
    setReadAloud,
    voiceInput,
    setVoiceInput,
    location,
    requestLocation,
    clearConversation,
    user,
    logout,
  } = useApp();
  const insets = useSafeAreaInsets();
  const tabBarHeight = useSafeTabBarHeight();

  const [mySevas, setMySevas] = useState<CommunityServiceItem[]>([]);
  const [isModalVisible, setIsModalVisible] = useState(false);
  const [isPublishing, setIsPublishing] = useState(false);
  const [isWithdrawing, setIsWithdrawing] = useState<string | null>(null);

  // Form State
  const [providerName, setProviderName] = useState(user?.name || '');
  const [category, setCategory] = useState<SevaCategory>('food');
  const [title, setTitle] = useState('');
  const [address, setAddress] = useState('');
  const [contactPhone, setContactPhone] = useState(user?.phoneNumber || '');
  const [selectedLat, setSelectedLat] = useState<number | null>(null);
  const [selectedLng, setSelectedLng] = useState<number | null>(null);
  const [searchResults, setSearchResults] = useState<{ placeName: string; center: [number, number] }[]>([]);
  const [isSearchingAddress, setIsSearchingAddress] = useState(false);

  // Load saved local Seva offerings
  useEffect(() => {
    AsyncStorage.getItem(MY_SEVAS_KEY)
      .then((res) => {
        if (res) setMySevas(JSON.parse(res));
      })
      .catch(() => undefined);
  }, []);

  const saveSevas = async (items: CommunityServiceItem[]) => {
    setMySevas(items);
    await AsyncStorage.setItem(MY_SEVAS_KEY, JSON.stringify(items)).catch(() => undefined);
  };

  const searchAddressQuery = async (query: string) => {
    setAddress(query);
    if (!query.trim() || query.length < 3) {
      setSearchResults([]);
      return;
    }
    const token = process.env.EXPO_PUBLIC_MAPBOX_TOKEN || '';
    if (!token) return;
    setIsSearchingAddress(true);
    try {
      const url = `https://api.mapbox.com/geocoding/v5/mapbox.places/${encodeURIComponent(query)}.json?access_token=${token}&country=IN&limit=4`;
      const res = await fetch(url);
      const data = await res.json();
      if (data && data.features) {
        const results = data.features.map((f: any) => ({
          placeName: f.place_name,
          center: f.center as [number, number], // [lng, lat]
        }));
        setSearchResults(results);
      }
    } catch {
      // Ignore network search errors
    } finally {
      setIsSearchingAddress(false);
    }
  };

  const selectPlace = (item: { placeName: string; center: [number, number] }) => {
    setAddress(item.placeName);
    setSelectedLng(item.center[0]);
    setSelectedLat(item.center[1]);
    setSearchResults([]);
  };

  const useCurrentGPS = () => {
    if (location.latitude && location.longitude) {
      setSelectedLat(location.latitude);
      setSelectedLng(location.longitude);
      setAddress((prev) => (prev.trim() ? prev : `Near ${location.latitude?.toFixed(4)}, ${location.longitude?.toFixed(4)}`));
      Alert.alert('GPS Location Set 🙏', 'Using your live GPS location for this Seva.');
    } else {
      void requestLocation();
    }
  };

  const handlePublishSeva = async () => {
    if (!title.trim() || !address.trim() || !contactPhone.trim() || !providerName.trim()) {
      Alert.alert('Missing Details', 'Please fill in all required fields to publish your Seva offering.');
      return;
    }

    setIsPublishing(true);
    try {
      const lat = selectedLat ?? location.latitude ?? 17.6778; // Default Pandharpur coordinates
      const lng = selectedLng ?? location.longitude ?? 75.3283;
      const now = new Date();
      const nextWeek = new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000);

      let newItem: CommunityServiceItem;
      try {
        newItem = await communityApi.publish({
          providerName: providerName.trim(),
          category,
          title: title.trim(),
          address: address.trim(),
          latitude: lat,
          longitude: lng,
          availableFrom: now.toISOString(),
          availableUntil: nextWeek.toISOString(),
          contactPhone: contactPhone.trim(),
        });
      } catch {
        // Fallback for offline mode / unreachable backend
        newItem = {
          id: `local-seva-${Date.now()}`,
          providerName: providerName.trim(),
          category,
          title: title.trim(),
          address: address.trim(),
          latitude: lat,
          longitude: lng,
          availableFrom: now.toISOString(),
          availableUntil: nextWeek.toISOString(),
          contactPhone: contactPhone.trim(),
          isActive: true,
          isOpenNow: true,
          createdAt: now.toISOString(),
          manageToken: `local-token-${Date.now()}`,
        };
      }

      const updated = [newItem, ...mySevas];
      await saveSevas(updated);

      setIsModalVisible(false);
      setTitle('');
      setAddress('');
      Alert.alert('Seva Published! 🙏', 'Your offering is now live on the map and AI assistant search for all pilgrims.');
    } catch {
      Alert.alert('Publishing Failed', 'Could not publish your Seva. Please try again.');
    } finally {
      setIsPublishing(false);
    }
  };

  const handleWithdraw = (item: CommunityServiceItem) => {
    Alert.alert(
      'Withdraw Seva',
      `Are you sure you want to take down "${item.title}"?`,
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Withdraw',
          style: 'destructive',
          onPress: async () => {
            setIsWithdrawing(item.id);
            try {
              await communityApi.withdraw(item.id, item.manageToken);
            } catch {
              // Server fallback handling
            }
            const updated = mySevas.filter((s) => s.id !== item.id);
            await saveSevas(updated);

            // Completely clear form inputs and close modal
            setTitle('');
            setAddress('');
            setProviderName('');
            setContactPhone('');
            setSelectedLat(null);
            setSelectedLng(null);
            setIsModalVisible(false);

            Alert.alert('Seva Withdrawn & Form Cleared 🗑️', 'Your charity service offering and form details have been completely removed.');
            setIsWithdrawing(null);
          },
        },
      ]
    );
  };
  const handleToggleLock = async (item: CommunityServiceItem) => {
    if (!user) {
      Alert.alert('Sign In Required', 'Please sign in to lock or reserve a Seva spot.', [
        { text: 'Cancel', style: 'cancel' },
        { text: 'Sign In', onPress: () => router.push('/auth') },
      ]);
      return;
    }
    try {
      let updatedItem: CommunityServiceItem;
      if (item.isLocked) {
        updatedItem = await communityApi.unlock(item.id);
        Alert.alert('Seva Unlocked', 'The spot is now available for other pilgrims.');
      } else {
        updatedItem = await communityApi.lock(item.id, user.name || 'Pilgrim', user.phoneNumber);
        Alert.alert('Seva Reserved! 🔒', `You have locked "${item.title}". Provider notified.`);
      }
      const updatedList = mySevas.map((s) => (s.id === item.id ? { ...s, ...updatedItem } : s));
      await saveSevas(updatedList);
    } catch {
      Alert.alert('Operation Failed', 'Could not update service locking status.');
    }
  };

  const handleLogout = async () => {
    await logout();
    router.replace('/auth');
  };

  const confirmClear = () =>
    Alert.alert(
      copy.clearConversation,
      'This removes the conversation saved on this device.',
      [
        { text: copy.cancel, style: 'cancel' },
        { text: 'Clear', style: 'destructive', onPress: () => void clearConversation() },
      ]
    );

  return (
    <ScrollView
      style={styles.screen}
      contentContainerStyle={[styles.content, { paddingTop: insets.top + 18, paddingBottom: tabBarHeight + 22 }]}
    >
      <Text style={styles.kicker}>WariVerse</Text>
      <Text style={styles.title}>{copy.settings}</Text>

      {/* --- ACCOUNT --- */}
      <SectionTitle label="Account" />
      {user ? (
        <View style={styles.locationRow}>
          <View style={styles.settingIcon}>
            <Feather name="user-check" size={17} color={colors.light.teal} />
          </View>
          <View style={styles.settingCopy}>
            <Text style={styles.settingTitle}>{user.name || 'Warkari'}</Text>
            <Text style={styles.settingDescription}>+91 {user.phoneNumber} · Verified</Text>
          </View>
          <Pressable onPress={() => void handleLogout()} style={styles.logoutBtn}>
            <Text style={styles.logoutText}>Sign Out</Text>
          </Pressable>
        </View>
      ) : (
        <Pressable accessibilityRole="button" onPress={() => router.push('/auth')} style={styles.locationRow}>
          <View style={styles.settingIcon}>
            <Feather name="user-plus" size={17} color={colors.light.teal} />
          </View>
          <View style={styles.settingCopy}>
            <Text style={styles.settingTitle}>Sign In / Register</Text>
            <Text style={styles.settingDescription}>Sign in with mobile number & OTP</Text>
          </View>
          <Feather name="chevron-right" size={17} color={colors.light.mutedForeground} />
        </Pressable>
      )}

      {/* --- HELPLINE --- */}
      <SectionTitle label="Helpline" />
      <Pressable
        accessibilityRole="button"
        onPress={() => router.push('/ivr-dialer')}
        style={styles.locationRow}
      >
        <View style={styles.settingIcon}>
          <Feather name="phone-call" size={17} color={colors.light.teal} />
        </View>
        <View style={styles.settingCopy}>
          <Text style={styles.settingTitle}>Call WariVerse Helpline</Text>
          <Text style={styles.settingDescription}>
            Menu-driven help over your data connection — no airtime used
          </Text>
        </View>
        <Feather name="chevron-right" size={17} color={colors.light.mutedForeground} />
      </Pressable>

      {/* --- COMMUNITY SEVA & CHARITY --- */}
      <SectionTitle label="Community Seva & Charity" />
      <Pressable
        accessibilityRole="button"
        onPress={() => {
          if (user) {
            setProviderName(user.name || '');
            setContactPhone(user.phoneNumber || '');
          }
          setIsModalVisible(true);
        }}
        style={styles.sevaRow}
      >
        <View style={styles.sevaIconBox}>
          <Feather name="heart" size={18} color={colors.light.white} />
        </View>
        <View style={styles.settingCopy}>
          <Text style={styles.sevaTitle}>Publish Free Seva / Charity</Text>
          <Text style={styles.settingDescription}>Offer free Annachhatra, water, or shelter along the route</Text>
        </View>
        <Feather name="plus-circle" size={20} color={colors.light.teal} />
      </Pressable>

      {/* User's Active Seva Offerings */}
      {mySevas.length > 0 && (
        <View style={styles.mySevasBox}>
          <Text style={styles.mySevasTitle}>YOUR PUBLISHED SEVA OFFERINGS</Text>
          {mySevas.map((item) => (
            <View key={item.id} style={styles.sevaItemCard}>
              <View style={styles.sevaItemHeader}>
                <View style={styles.sevaBadge}>
                  <Text style={styles.sevaBadgeText}>{item.category.toUpperCase()}</Text>
                </View>
                <View style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
                  <Pressable
                    onPress={() => handleToggleLock(item)}
                    style={[styles.lockBtn, item.isLocked && styles.lockBtnActive]}
                  >
                    <Feather name={item.isLocked ? "lock" : "unlock"} size={12} color={item.isLocked ? "#991b1b" : colors.light.teal} />
                    <Text style={[styles.lockText, item.isLocked && styles.lockTextActive]}>
                      {item.isLocked ? "Locked" : "Lock Spot"}
                    </Text>
                  </Pressable>
                  <Pressable
                    disabled={isWithdrawing === item.id}
                    onPress={() => handleWithdraw(item)}
                    style={styles.withdrawBtn}
                  >
                    {isWithdrawing === item.id ? (
                      <ActivityIndicator size="small" color={colors.light.destructive} />
                    ) : (
                      <Text style={styles.withdrawText}>Withdraw</Text>
                    )}
                  </Pressable>
                </View>
              </View>
              <Text style={styles.sevaItemTitle}>{item.title}</Text>
              <Text style={styles.sevaItemSub}>
                {item.providerName} · {item.address}
              </Text>
              {item.isLocked && (
                <View style={styles.lockedNotice}>
                  <Feather name="info" size={12} color="#991b1b" />
                  <Text style={styles.lockedNoticeText}>
                    Reserved by {item.lockedByName || 'Pilgrim'} ({item.lockedByPhone || 'Verified'})
                  </Text>
                </View>
              )}
            </View>
          ))}
        </View>
      )}

      {/* --- LANGUAGE --- */}
      <SectionTitle label={copy.language} />
      <View style={styles.languageRow}>
        {languages.map((item) => (
          <Pressable
            key={item.id}
            accessibilityRole="radio"
            accessibilityState={{ selected: language === item.id }}
            onPress={() => setLanguage(item.id)}
            style={({ pressed }) => [
              styles.languageOption,
              language === item.id && styles.languageActive,
              pressed && { opacity: 0.7 },
            ]}
          >
            <Text style={[styles.languageLabel, language === item.id && styles.languageLabelActive]}>
              {item.label}
            </Text>
            {language === item.id && <Feather name="check" size={15} color={colors.light.white} />}
          </Pressable>
        ))}
      </View>

      {/* --- PREFERENCES --- */}
      <SectionTitle label="Preferences" />
      <SettingRow
        icon="volume-2"
        title={copy.readAloud}
        description="Play assistant answers when available"
        value={readAloud}
        onChange={setReadAloud}
      />
      <SettingRow
        icon="mic"
        title={copy.voiceInput}
        description="Use the microphone in chat"
        value={voiceInput}
        onChange={setVoiceInput}
      />

      {/* --- LOCATION --- */}
      <SectionTitle label={copy.location} />
      <Pressable accessibilityRole="button" onPress={() => void requestLocation()} style={styles.locationRow}>
        <View style={styles.settingIcon}>
          <Feather name="map-pin" size={17} color={colors.light.teal} />
        </View>
        <View style={styles.settingCopy}>
          <Text style={styles.settingTitle}>
            {location.permission === 'granted' ? 'Location enabled' : 'Location permission'}
          </Text>
          <Text style={styles.settingDescription}>
            {location.permission === 'granted' ? 'Ready for routes, facilities, and SOS' : copy.allowLocation}
          </Text>
        </View>
        <Feather name="chevron-right" size={17} color={colors.light.mutedForeground} />
      </Pressable>
      {location.permission === 'denied' && <Text style={styles.denied}>{copy.locationDenied}</Text>}

      {/* --- SESSION --- */}
      <SectionTitle label="Session" />
      <Pressable accessibilityRole="button" onPress={confirmClear} style={styles.locationRow}>
        <View style={[styles.settingIcon, styles.dangerIcon]}>
          <Feather name="trash-2" size={17} color={colors.light.destructive} />
        </View>
        <View style={styles.settingCopy}>
          <Text style={styles.settingTitle}>{copy.clearConversation}</Text>
          <Text style={styles.settingDescription}>Remove saved messages from this device</Text>
        </View>
        <Feather name="chevron-right" size={17} color={colors.light.mutedForeground} />
      </Pressable>

      <View style={styles.about}>
        <View style={styles.aboutMark}>
          <Feather name="navigation" size={17} color={colors.light.white} />
        </View>
        <Text style={styles.aboutName}>WariVerse</Text>
        <Text style={styles.aboutText}>A kinder way to find your way through the Wari.</Text>
        <Text style={styles.version}>Version 1.0 · Frontend demo</Text>
      </View>

      {/* --- SEVA PUBLISH MODAL --- */}
      <Modal visible={isModalVisible} animationType="slide" transparent onRequestClose={() => setIsModalVisible(false)}>
        <View style={styles.modalOverlay}>
          <View style={styles.modalContent}>
            <View style={styles.modalHeader}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                <View style={styles.sevaIconBox}>
                  <Feather name="heart" size={16} color={colors.light.white} />
                </View>
                <Text style={styles.modalTitle}>Publish Seva / Charity</Text>
              </View>
              <Pressable onPress={() => setIsModalVisible(false)}>
                <Feather name="x" size={22} color={colors.light.mutedForeground} />
              </Pressable>
            </View>

            <ScrollView style={{ maxHeight: 420 }} showsVerticalScrollIndicator={false}>
              <Text style={styles.fieldLabel}>CATEGORY</Text>
              <ScrollView horizontal showsHorizontalScrollIndicator={false} style={styles.catRow}>
                {SEVA_CATEGORIES.map((cat) => (
                  <Pressable
                    key={cat.id}
                    onPress={() => setCategory(cat.id)}
                    style={[styles.catPill, category === cat.id && styles.catPillActive]}
                  >
                    <Feather
                      name={cat.icon}
                      size={13}
                      color={category === cat.id ? colors.light.white : colors.light.teal}
                    />
                    <Text style={[styles.catPillText, category === cat.id && styles.catPillTextActive]}>
                      {cat.label}
                    </Text>
                  </Pressable>
                ))}
              </ScrollView>

              <Text style={styles.fieldLabel}>ORGANIZER / PROVIDER NAME *</Text>
              <TextInput
                style={styles.input}
                placeholder="e.g. Shri Ram Seva Mandal"
                placeholderTextColor="#9ca3af"
                value={providerName}
                onChangeText={setProviderName}
              />

              <Text style={styles.fieldLabel}>SEVA TITLE *</Text>
              <TextInput
                style={styles.input}
                placeholder="e.g. Free Tea & Warm Breakfast"
                placeholderTextColor="#9ca3af"
                value={title}
                onChangeText={setTitle}
              />

              <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginTop: 10 }}>
                <Text style={styles.fieldLabel}>ADDRESS / SEARCH LOCATION *</Text>
                <Pressable onPress={useCurrentGPS} style={{ flexDirection: 'row', alignItems: 'center', gap: 4 }}>
                  <Feather name="crosshair" size={13} color={colors.light.teal} />
                  <Text style={{ fontSize: 11, fontWeight: '600', color: colors.light.teal }}>Use GPS</Text>
                </Pressable>
              </View>
              <TextInput
                style={styles.input}
                placeholder="Search address (e.g. FC Road Pune, Hadapsar)"
                placeholderTextColor="#9ca3af"
                value={address}
                onChangeText={searchAddressQuery}
              />
              {isSearchingAddress && (
                <ActivityIndicator size="small" color={colors.light.teal} style={{ marginVertical: 4 }} />
              )}
              {searchResults.length > 0 && (
                <View style={{ backgroundColor: '#f3f4f6', borderRadius: 8, padding: 6, marginBottom: 8 }}>
                  {searchResults.map((item, idx) => (
                    <Pressable
                      key={idx}
                      onPress={() => selectPlace(item)}
                      style={{ paddingVertical: 6, paddingHorizontal: 8, borderBottomWidth: idx === searchResults.length - 1 ? 0 : 1, borderBottomColor: '#e5e7eb' }}
                    >
                      <Text style={{ fontSize: 12, color: colors.light.foreground, fontWeight: '500' }}>{item.placeName}</Text>
                    </Pressable>
                  ))}
                </View>
              )}

              <Text style={styles.fieldLabel}>CONTACT MOBILE NUMBER *</Text>
              <TextInput
                style={styles.input}
                placeholder="e.g. 9876543210"
                placeholderTextColor="#9ca3af"
                keyboardType="phone-pad"
                value={contactPhone}
                onChangeText={setContactPhone}
              />
            </ScrollView>

            <View style={styles.modalFooter}>
              <Pressable
                disabled={isPublishing}
                onPress={() => void handlePublishSeva()}
                style={styles.submitBtn}
              >
                {isPublishing ? (
                  <ActivityIndicator color={colors.light.white} />
                ) : (
                  <>
                    <Feather name="check-circle" size={17} color={colors.light.white} />
                    <Text style={styles.submitBtnText}>Publish Seva Offering</Text>
                  </>
                )}
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>
    </ScrollView>
  );
}

function SectionTitle({ label }: { label: string }) {
  return <Text style={styles.sectionTitle}>{label}</Text>;
}

function SettingRow({
  icon,
  title,
  description,
  value,
  onChange,
}: {
  icon: keyof typeof Feather.glyphMap;
  title: string;
  description: string;
  value: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <View style={styles.locationRow}>
      <View style={styles.settingIcon}>
        <Feather name={icon} size={17} color={colors.light.teal} />
      </View>
      <View style={styles.settingCopy}>
        <Text style={styles.settingTitle}>{title}</Text>
        <Text style={styles.settingDescription}>{description}</Text>
      </View>
      <Switch
        accessibilityLabel={title}
        value={value}
        onValueChange={onChange}
        trackColor={{ false: colors.light.input, true: '#9dc7b4' }}
        thumbColor={value ? colors.light.teal : colors.light.white}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.light.background },
  content: { paddingHorizontal: 18 },
  kicker: { color: colors.light.mutedForeground, fontFamily: 'Inter_600SemiBold', fontSize: 10, letterSpacing: 1 },
  title: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 29, letterSpacing: -0.8, marginTop: 4 },
  sectionTitle: {
    color: colors.light.mutedForeground,
    fontFamily: 'Inter_700Bold',
    fontSize: 10,
    letterSpacing: 1,
    textTransform: 'uppercase',
    marginTop: 27,
    marginBottom: 10,
  },
  languageRow: { flexDirection: 'row', gap: 8 },
  languageOption: {
    minHeight: 42,
    backgroundColor: colors.light.card,
    borderWidth: 1,
    borderColor: colors.light.border,
    borderRadius: 13,
    paddingHorizontal: 12,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
  },
  languageActive: { backgroundColor: colors.light.teal, borderColor: colors.light.teal },
  languageLabel: { color: colors.light.teal, fontFamily: 'Inter_600SemiBold', fontSize: 12 },
  languageLabelActive: { color: colors.light.white },
  locationRow: {
    backgroundColor: colors.light.card,
    borderWidth: 1,
    borderColor: colors.light.border,
    borderRadius: 17,
    minHeight: 67,
    padding: 11,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 10,
    marginBottom: 9,
  },
  settingIcon: {
    width: 35,
    height: 35,
    borderRadius: 12,
    backgroundColor: colors.light.tealSoft,
    alignItems: 'center',
    justifyContent: 'center',
  },
  dangerIcon: { backgroundColor: '#fae4df' },
  settingCopy: { flex: 1 },
  settingTitle: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 12 },
  settingDescription: { color: colors.light.mutedForeground, fontFamily: 'Inter_400Regular', fontSize: 10, marginTop: 3 },
  logoutBtn: { backgroundColor: '#fae4df', borderRadius: 10, paddingHorizontal: 10, paddingVertical: 6 },
  logoutText: { color: colors.light.destructive, fontFamily: 'Inter_600SemiBold', fontSize: 11 },
  denied: { color: colors.light.destructive, fontFamily: 'Inter_400Regular', fontSize: 11, marginTop: 0 },
  about: { alignItems: 'center', paddingVertical: 27 },
  aboutMark: {
    width: 35,
    height: 35,
    borderRadius: 12,
    backgroundColor: colors.light.primary,
    alignItems: 'center',
    justifyContent: 'center',
    transform: [{ rotate: '-10deg' }],
  },
  aboutName: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 15, marginTop: 9 },
  aboutText: { color: colors.light.mutedForeground, fontFamily: 'Inter_400Regular', fontSize: 11, marginTop: 5 },
  version: { color: colors.light.mutedForeground, fontFamily: 'Inter_400Regular', fontSize: 9, marginTop: 10 },

  /* Community Seva Styles */
  sevaRow: {
    backgroundColor: colors.light.tealSoft,
    borderWidth: 1,
    borderColor: '#b2d8c6',
    borderRadius: 17,
    minHeight: 72,
    padding: 13,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    marginBottom: 9,
  },
  sevaIconBox: {
    width: 38,
    height: 38,
    borderRadius: 13,
    backgroundColor: colors.light.teal,
    alignItems: 'center',
    justifyContent: 'center',
  },
  sevaTitle: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 13 },
  mySevasBox: {
    backgroundColor: colors.light.card,
    borderWidth: 1,
    borderColor: colors.light.border,
    borderRadius: 16,
    padding: 13,
    marginTop: 4,
    marginBottom: 9,
  },
  mySevasTitle: {
    color: colors.light.mutedForeground,
    fontFamily: 'Inter_700Bold',
    fontSize: 9,
    letterSpacing: 0.8,
    marginBottom: 10,
  },
  sevaItemCard: {
    backgroundColor: '#f8faf9',
    borderWidth: 1,
    borderColor: '#e2e8f0',
    borderRadius: 12,
    padding: 10,
    marginBottom: 8,
  },
  sevaItemHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  sevaBadge: { backgroundColor: colors.light.tealSoft, borderRadius: 6, paddingHorizontal: 7, paddingVertical: 3 },
  sevaBadgeText: { color: colors.light.teal, fontFamily: 'Inter_700Bold', fontSize: 9 },
  withdrawBtn: { backgroundColor: '#fee2e2', borderRadius: 6, paddingHorizontal: 8, paddingVertical: 4 },
  withdrawText: { color: colors.light.destructive, fontFamily: 'Inter_600SemiBold', fontSize: 10 },
  lockBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    backgroundColor: '#ccfbf1',
    borderColor: '#0d9488',
    borderWidth: 1,
    borderRadius: 6,
    paddingHorizontal: 8,
    paddingVertical: 4,
  },
  lockBtnActive: {
    backgroundColor: '#fee2e2',
    borderColor: '#ef4444',
  },
  lockText: { color: colors.light.teal, fontFamily: 'Inter_600SemiBold', fontSize: 10 },
  lockTextActive: { color: '#991b1b' },
  lockedNotice: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    backgroundColor: '#fef2f2',
    borderRadius: 6,
    padding: 6,
    marginTop: 6,
  },
  lockedNoticeText: { color: '#991b1b', fontFamily: 'Inter_500Medium', fontSize: 10 },
  sevaItemTitle: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 12, marginTop: 5 },
  sevaItemSub: { color: colors.light.mutedForeground, fontFamily: 'Inter_400Regular', fontSize: 10, marginTop: 2 },

  /* Modal Styles */
  modalOverlay: { flex: 1, backgroundColor: 'rgba(0,0,0,0.5)', justifyContent: 'flex-end' },
  modalContent: {
    backgroundColor: colors.light.background,
    borderTopLeftRadius: 24,
    borderTopRightRadius: 24,
    padding: 20,
    maxHeight: '85%',
  },
  modalHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 },
  modalTitle: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 17 },
  fieldLabel: {
    color: colors.light.mutedForeground,
    fontFamily: 'Inter_700Bold',
    fontSize: 10,
    letterSpacing: 0.8,
    marginTop: 14,
    marginBottom: 6,
  },
  catRow: { flexDirection: 'row', marginBottom: 6 },
  catPill: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    backgroundColor: colors.light.card,
    borderWidth: 1,
    borderColor: colors.light.border,
    borderRadius: 12,
    paddingHorizontal: 12,
    paddingVertical: 8,
    marginRight: 8,
  },
  catPillActive: { backgroundColor: colors.light.teal, borderColor: colors.light.teal },
  catPillText: { color: colors.light.teal, fontFamily: 'Inter_600SemiBold', fontSize: 11 },
  catPillTextActive: { color: colors.light.white },
  input: {
    backgroundColor: colors.light.card,
    borderWidth: 1,
    borderColor: colors.light.border,
    borderRadius: 12,
    paddingHorizontal: 13,
    paddingVertical: 10,
    color: colors.light.foreground,
    fontFamily: 'Inter_500Medium',
    fontSize: 13,
  },
  modalFooter: { marginTop: 18, paddingTop: 12, borderTopWidth: 1, borderTopColor: colors.light.border },
  submitBtn: {
    backgroundColor: colors.light.teal,
    borderRadius: 14,
    minHeight: 48,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 8,
  },
  submitBtnText: { color: colors.light.white, fontFamily: 'Inter_700Bold', fontSize: 14 },
});