import Constants from 'expo-constants';
import type { ConversationResponse, Language, ToolWidget } from '@/types/domain';

/**
 * Real backend client.
 *
 * The backend speaks snake_case; this app speaks camelCase. The conversion is
 * done here, once, with a recursive key transform rather than field-by-field.
 *
 * Why not camelCase aliases on the backend instead? Because widget `data` is an
 * untyped dict there — Pydantic aliases only rename declared model fields, so
 * `zone_id`, `updated_at`, `route_coordinates` and the rest would come through
 * unchanged no matter what aliases were configured. A transform on this side is
 * the only thing that covers them.
 */

export type MessageRequest = {
  sessionId: string;
  language: Language;
  message: string;
  isVoice?: boolean;
  latitude?: number | null;
  longitude?: number | null;
};

export const DEFAULT_SESSION_ID = 'wariverse-session';

/** Milliseconds before a request is abandoned. Pilgrims are often on 2G. */
const REQUEST_TIMEOUT_MS = 20_000;

export function getApiBaseUrl(): string {
  const configured = process.env.EXPO_PUBLIC_API_URL;
  if (configured) return configured.replace(/\/+$/, '');

  // On a physical device `localhost` is the phone itself, so fall back to the
  // host running Metro.
  const hostUri =
    Constants.expoConfig?.hostUri ??
    (Constants as any).manifest2?.extra?.expoGo?.debuggerHost;
  if (hostUri) {
    const host = String(hostUri).split(':')[0];
    if (host && host !== 'localhost' && host !== '127.0.0.1') {
      return `http://${host}:8000`;
    }
  }
  return 'http://localhost:8000';
}

/* -------------------------------------------------------------------------- */
/* snake_case → camelCase                                                      */
/* -------------------------------------------------------------------------- */

function toCamel(key: string): string {
  return key.replace(/_([a-z0-9])/g, (_, character: string) => character.toUpperCase());
}

/**
 * Deep-converts object keys. Arrays are walked, primitives pass through, and
 * `Date` is left alone — though the backend sends strings, so it should not
 * appear.
 */
export function camelizeKeys<T = unknown>(input: unknown): T {
  if (Array.isArray(input)) {
    return input.map((item) => camelizeKeys(item)) as unknown as T;
  }
  if (input !== null && typeof input === 'object') {
    const output: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(input as Record<string, unknown>)) {
      output[toCamel(key)] = camelizeKeys(value);
    }
    return output as T;
  }
  return input as T;
}

/* -------------------------------------------------------------------------- */
/* transport                                                                   */
/* -------------------------------------------------------------------------- */

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly requestId?: string
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(
  path: string,
  body: unknown,
  token?: string | null
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const response = await fetch(`${getApiBaseUrl()}${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });

    const payload = await response.json().catch(() => null);

    if (!response.ok) {
      // The backend wraps errors as { error: { code, message }, request_id }.
      const message =
        payload?.error?.message ?? `Request failed with status ${response.status}`;
      throw new ApiError(message, response.status, payload?.request_id);
    }
    return camelizeKeys<T>(payload);
  } finally {
    clearTimeout(timer);
  }
}

type QueryValue = string | number | undefined | (string | number)[];

async function getJson<T>(path: string, params: Record<string, QueryValue> = {}): Promise<T> {
  const pairs: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    // FastAPI reads repeated keys as a list, which is how `category` asks for
    // several facility types at once.
    for (const item of Array.isArray(value) ? value : [value]) {
      pairs.push(`${key}=${encodeURIComponent(String(item))}`);
    }
  }
  const query = pairs.join('&');

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(
      `${getApiBaseUrl()}${path}${query ? `?${query}` : ''}`,
      { signal: controller.signal }
    );
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ApiError(
        payload?.error?.message ?? `Request failed with status ${response.status}`,
        response.status,
        payload?.request_id
      );
    }
    return camelizeKeys<T>(payload);
  } finally {
    clearTimeout(timer);
  }
}

/* -------------------------------------------------------------------------- */
/* endpoints                                                                   */
/* -------------------------------------------------------------------------- */

export type CrowdZoneReading = {
  zoneId: string;
  zoneName: string;
  density: number;
  status: 'LOW' | 'MODERATE' | 'HIGH' | 'VERY_HIGH';
  latitude?: number | null;
  longitude?: number | null;
  waitMinutes?: number | null;
  updatedAt?: string;
  recommendation?: string | null;
};

export type NearbyFacility = {
  id?: string;
  name: string;
  category: string;
  latitude?: number | null;
  longitude?: number | null;
  distance?: string;
  distanceM?: number;
  availability?: string;
  contact?: string;
  phone?: string;
  isSeva?: boolean;
  providerName?: string;
};

export const crowdApi = {
  /** Live density for every zone. Drives the map's colour-coded pins. */
  async all(language: Language = 'en'): Promise<CrowdZoneReading[]> {
    const payload = await getJson<{ zones?: CrowdZoneReading[] } | CrowdZoneReading[]>(
      '/api/crowd/all',
      { language }
    );
    return Array.isArray(payload) ? payload : (payload.zones ?? []);
  },
};

export const facilitiesApi = {
  /**
   * Facilities around a point. `radiusM` defaults to the 10 km the map uses —
   * wide enough to cover the whole Pandharpur approach, where a pilgrim two
   * villages out still needs to see the medical post ahead of them.
   */
  async nearby(input: {
    latitude: number;
    longitude: number;
    radiusM?: number;
    category?: string[];
    limit?: number;
    language?: Language;
  }): Promise<NearbyFacility[]> {
    const payload = await getJson<{ facilities?: NearbyFacility[] } | NearbyFacility[]>(
      '/api/facilities/nearby',
      {
        lat: input.latitude,
        lng: input.longitude,
        radius_m: input.radiusM ?? 10_000,
        category: input.category,
        limit: input.limit ?? 50,
        language: input.language ?? 'en',
      }
    );
    return Array.isArray(payload) ? payload : (payload.facilities ?? []);
  },
};

export const conversationApi = {
  async sendMessage(
    input: MessageRequest,
    token?: string | null
  ): Promise<ConversationResponse> {
    const body: Record<string, unknown> = {
      session_id: input.sessionId,
      language: input.language,
      message: input.message,
      is_voice: input.isVoice ?? false,
    };
    // Only send coordinates we actually have — null would fail validation.
    if (typeof input.latitude === 'number' && typeof input.longitude === 'number') {
      body.latitude = input.latitude;
      body.longitude = input.longitude;
    }
    return request<ConversationResponse>('/api/conversation/message', body, token);
  },

  async confirmSOS(
    language: Language,
    sessionId: string = DEFAULT_SESSION_ID,
    token?: string | null
  ): Promise<ConversationResponse> {
    return request<ConversationResponse>(
      '/api/conversation/sos/confirm',
      { session_id: sessionId, language },
      token
    );
  },
};

export type CommunityServiceInput = {
  providerName: string;
  category: 'food' | 'accommodation' | 'water' | 'medical' | 'rest';
  title: string;
  address: string;
  latitude: number;
  longitude: number;
  availableFrom: string;
  availableUntil: string;
  contactPhone: string;
};

export type CommunityServiceItem = {
  id: string;
  providerName: string;
  category: string;
  title: string;
  address: string;
  latitude: number;
  longitude: number;
  availableFrom: string;
  availableUntil: string;
  contactPhone: string;
  isActive: boolean;
  isOpenNow: boolean;
  distanceM?: number | null;
  isLocked?: boolean;
  lockedByName?: string | null;
  lockedByPhone?: string | null;
  lockedAt?: string | null;
  createdAt: string;
  manageToken?: string;
};

export const authApi = {
  async requestOTP(phoneNumber: string): Promise<{ success: boolean; message: string; demoOtp?: string }> {
    const response = await fetch(`${getApiBaseUrl()}/api/auth/otp/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone_number: phoneNumber }),
    });
    const payload = await response.json();
    return {
      success: response.ok,
      message: payload.detail || payload.message || 'OTP Sent',
      demoOtp: payload.demo_otp || payload.demoOtp,
    };
  },

  async verifyOTP(phoneNumber: string, otp: string): Promise<{ success: boolean; token?: string; user?: any }> {
    const response = await fetch(`${getApiBaseUrl()}/api/auth/otp/verify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone_number: phoneNumber, otp }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || 'OTP verification failed');
    }
    const data = camelizeKeys<any>(payload);
    return { success: true, token: data.token, user: data.user };
  },
};

export const communityApi = {
  async publish(input: CommunityServiceInput): Promise<CommunityServiceItem> {
    const body = {
      provider_name: input.providerName,
      category: input.category,
      title: input.title,
      address: input.address,
      latitude: input.latitude,
      longitude: input.longitude,
      available_from: input.availableFrom,
      available_until: input.availableUntil,
      contact_phone: input.contactPhone,
    };
    return request<CommunityServiceItem>('/api/community/services', body);
  },

  async list(lat?: number, lng?: number): Promise<{ services: CommunityServiceItem[] }> {
    const params = new URLSearchParams();
    if (lat !== undefined && lng !== undefined) {
      params.append('lat', String(lat));
      params.append('lng', String(lng));
    }
    const response = await fetch(`${getApiBaseUrl()}/api/community/services?${params.toString()}`);
    const payload = await response.json().catch(() => ({ services: [] }));
    return camelizeKeys<{ services: CommunityServiceItem[] }>(payload);
  },

  async withdraw(serviceId: string, manageToken?: string): Promise<void> {
    const headers: Record<string, string> = {};
    if (manageToken) {
      headers['X-Manage-Token'] = manageToken;
    }
    await fetch(`${getApiBaseUrl()}/api/community/services/${serviceId}`, {
      method: 'DELETE',
      headers,
    });
  },

  async lock(serviceId: string, name?: string, phone?: string): Promise<CommunityServiceItem> {
    const params = new URLSearchParams();
    if (name) params.append('name', name);
    if (phone) params.append('phone', phone);
    const response = await fetch(`${getApiBaseUrl()}/api/community/services/${serviceId}/lock?${params.toString()}`, {
      method: 'POST',
    });
    const payload = await response.json();
    return camelizeKeys<CommunityServiceItem>(payload);
  },

  async unlock(serviceId: string): Promise<CommunityServiceItem> {
    const response = await fetch(`${getApiBaseUrl()}/api/community/services/${serviceId}/unlock`, {
      method: 'POST',
    });
    const payload = await response.json();
    return camelizeKeys<CommunityServiceItem>(payload);
  },
};

export type PalkhiLocation = {
  latitude: number;
  longitude: number;
  currentPlace: string;
  nextPlace: string;
  etaMinutes: number;
  updatedAt: string;
  isSimulated: boolean;
};

export const palkhiApi = {
  async getLiveLocation(): Promise<PalkhiLocation> {
    const response = await fetch(`${getApiBaseUrl()}/api/palkhi/live`).catch(() => null);
    if (!response || !response.ok) {
      return {
        latitude: 18.5204,
        longitude: 73.8567,
        currentPlace: 'Pune (Sangamwadi Halt)',
        nextPlace: 'Hadapsar Palkhi Sthal',
        etaMinutes: 25,
        updatedAt: new Date().toISOString(),
        isSimulated: true,
      };
    }
    const payload = await response.json().catch(() => null);
    return camelizeKeys<PalkhiLocation>(payload);
  },
};

export type { ToolWidget };
