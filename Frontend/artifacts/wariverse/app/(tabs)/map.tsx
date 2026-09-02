import { Feather } from '@expo/vector-icons';
import { useLocalSearchParams } from 'expo-router';
import React from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import colors from '@/constants/colors';
import { MapCanvas } from '@/components/MapCanvas';
import { useBottomTabBarHeight } from '@react-navigation/bottom-tabs';
import { useApp } from '@/store/AppContext';

function useSafeTabBarHeight() {
  try {
    return useBottomTabBarHeight();
  } catch {
    return 60;
  }
}

export default function MapScreen() {
  const { focus, destLat, destLng, destName, phone } = useLocalSearchParams<{
    focus?: string;
    destLat?: string;
    destLng?: string;
    destName?: string;
    phone?: string;
  }>();
  const { copy, location, requestLocation } = useApp();
  const insets = useSafeAreaInsets();
  const tabBarHeight = useSafeTabBarHeight();
  const routeMode = focus === 'route' || Boolean(destLat && destLng);
  return (
    <View style={[styles.screen, { paddingTop: insets.top + 15, paddingBottom: tabBarHeight + 12 }]}>
      <View style={styles.header}>
        <View>
          <Text style={styles.kicker}>{copy.liveMap.toUpperCase()}</Text>
          <Text style={styles.title}>{destName ? `Route: ${destName}` : routeMode ? copy.route : copy.crowd}</Text>
        </View>
        <View style={styles.livePill}>
          <View style={styles.liveDot} />
          <Text style={styles.liveText}>LIVE</Text>
        </View>
      </View>
      <View style={styles.mapWrap}>
        <MapCanvas
          mode={routeMode ? 'route' : 'crowd'}
          location={location}
          destLat={destLat ? parseFloat(destLat) : undefined}
          destLng={destLng ? parseFloat(destLng) : undefined}
          destName={destName}
          destPhone={phone}
          onRecenter={() => void requestLocation()}
        />
      </View>
      {location.permission !== 'granted' && (
        <Pressable
          accessibilityRole="button"
          onPress={() => void requestLocation()}
          style={({ pressed }) => [styles.locationPrompt, pressed && { opacity: 0.7 }]}
        >
          <View style={styles.locationIcon}>
            <Feather name="map-pin" size={17} color={colors.light.teal} />
          </View>
          <View style={styles.locationCopy}>
            <Text style={styles.locationTitle}>{copy.location}</Text>
            <Text style={styles.locationDescription}>{copy.allowLocation}</Text>
          </View>
          <Feather name="arrow-right" size={17} color={colors.light.teal} />
        </Pressable>
      )}
      {routeMode ? (
        <View style={styles.legend}>
          <View style={styles.legendRow}>
            <View style={[styles.legendDot, { backgroundColor: colors.light.teal }]} />
            <Text style={styles.legendText}>Live GPS</Text>
          </View>
          <View style={styles.legendRow}>
            <View style={[styles.legendLine, { backgroundColor: '#0d9488' }]} />
            <Text style={styles.legendText}>Walking route</Text>
          </View>
          <View style={styles.legendRow}>
            <View style={[styles.legendDot, { backgroundColor: '#0d9488' }]} />
            <Text style={styles.legendText}>{destName ? destName : 'Destination'}</Text>
          </View>
        </View>
      ) : (
        <View style={styles.legend}>
          <Text style={styles.legendHeading}>Crowd right now</Text>
          <View style={styles.legendRow}>
            <View style={[styles.legendDot, { backgroundColor: '#6d9b78' }]} />
            <Text style={styles.legendText}>Low</Text>
          </View>
          <View style={styles.legendRow}>
            <View style={[styles.legendDot, { backgroundColor: '#d59e2e' }]} />
            <Text style={styles.legendText}>Moderate</Text>
          </View>
          <View style={styles.legendRow}>
            <View style={[styles.legendDot, { backgroundColor: colors.light.primary }]} />
            <Text style={styles.legendText}>High</Text>
          </View>
        </View>
      )}
      <Text style={styles.disclaimer}>
        Live crowd and facilities · refreshed every minute
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.light.background, paddingHorizontal: 18 },
  header: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-end', marginBottom: 16 },
  kicker: { color: colors.light.mutedForeground, fontFamily: 'Inter_600SemiBold', fontSize: 10, letterSpacing: 1 },
  title: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 28, letterSpacing: -0.7, marginTop: 4 },
  livePill: { backgroundColor: colors.light.tealSoft, borderRadius: 14, paddingHorizontal: 9, paddingVertical: 7, flexDirection: 'row', alignItems: 'center', gap: 5 },
  liveDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: colors.light.teal },
  liveText: { color: colors.light.teal, fontFamily: 'Inter_700Bold', fontSize: 9, letterSpacing: 1 },
  mapWrap: { flex: 1 },
  locationPrompt: { backgroundColor: colors.light.card, borderWidth: 1, borderColor: colors.light.border, borderRadius: 17, padding: 12, flexDirection: 'row', alignItems: 'center', gap: 10, marginTop: 12 },
  locationIcon: { width: 35, height: 35, borderRadius: 12, backgroundColor: colors.light.tealSoft, alignItems: 'center', justifyContent: 'center' },
  locationCopy: { flex: 1 },
  locationTitle: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 12 },
  locationDescription: { color: colors.light.mutedForeground, fontFamily: 'Inter_400Regular', fontSize: 11, marginTop: 2 },
  legend: { backgroundColor: colors.light.card, borderRadius: 17, borderWidth: 1, borderColor: colors.light.border, padding: 13, flexDirection: 'row', alignItems: 'center', gap: 13, flexWrap: 'wrap', marginTop: 12 },
  legendHeading: { color: colors.light.foreground, fontFamily: 'Inter_700Bold', fontSize: 11, width: '100%' },
  legendRow: { flexDirection: 'row', alignItems: 'center', gap: 5 },
  legendDot: { width: 8, height: 8, borderRadius: 4 },
  legendLine: { width: 17, height: 4, borderRadius: 2 },
  legendText: { color: colors.light.inkSoft, fontFamily: 'Inter_500Medium', fontSize: 10 },
  disclaimer: { color: colors.light.mutedForeground, fontFamily: 'Inter_400Regular', fontSize: 9, textAlign: 'center', marginTop: 9 },
});