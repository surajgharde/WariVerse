export type Language = 'mr' | 'hi' | 'en';

export type User = {
  id: string;
  phoneNumber: string;
  name?: string;
  isAuthenticated: boolean;
  token?: string;
  createdAt: string;
};

export type MessageRole = 'user' | 'assistant' | 'system';

export type CrowdStatus = 'LOW' | 'MODERATE' | 'HIGH' | 'VERY_HIGH';

export type ToolWidget =
  | CrowdDensityWidget
  | ForecastWidget
  | RouteWidget
  | FacilityWidget
  | TempleInfoWidget
  | LostFoundWidget
  | SOSWidget
  | EscalationWidget
  | PalkhiLocationWidget;

export type PalkhiLocationWidget = {
  type: 'palkhi_location';
  data: {
    latitude: number;
    longitude: number;
    currentPlace: string;
    nextPlace: string;
    etaMinutes: number;
    updatedAt: string;
    isSimulated: boolean;
    palkhiName?: string;
    chiefName?: string;
    chiefPhone?: string;
    nodalOfficer?: string;
    nodalPhone?: string;
    policePortalUrl?: string;
  };
};

export type Message = {
  id: string;
  role: MessageRole;
  text?: string;
  timestamp: string;
  language?: Language;
  isVoice?: boolean;
  widgets?: ToolWidget[];
};

export type ConversationResponse = {
  sessionId: string;
  messageId: string;
  language: Language;
  responseText: string;
  widgets?: ToolWidget[];
};

export type CrowdDensityWidget = {
  type: 'crowd_density';
  data: {
    zoneId: string;
    zoneName: string;
    density: number;
    status: CrowdStatus;
    latitude?: number;
    longitude?: number;
    updatedAt: string;
  };
};

export type ForecastWidget = {
  type: 'congestion_forecast';
  data: {
    zoneId: string;
    zoneName: string;
    points: { time: string; value: number }[];
    recommendation?: string;
    updatedAt: string;
  };
};

export type RouteWidget = {
  type: 'route_guidance';
  data: {
    origin: { latitude: number; longitude: number; label?: string };
    destination: { latitude: number; longitude: number; label?: string };
    routeCoordinates: { latitude: number; longitude: number }[];
    estimatedTime?: string;
    distance?: string;
    avoidAreas?: string[];
  };
};

export type FacilityWidget = {
  type: 'nearby_facility';
  data: {
    id?: string;
    category: 'medical' | 'water' | 'toilet' | 'rest' | 'food' | 'accommodation' | 'police';
    name: string;
    distance?: string;
    latitude?: number;
    longitude?: number;
    availability?: string;
    contact?: string;
    phone?: string;
    isSeva?: boolean;
    isCharity?: boolean;
    providerName?: string;
    isLocked?: boolean;
    lockedByName?: string;
    lockedByPhone?: string;
  };
};

export type TempleInfoWidget = {
  type: 'temple_info';
  data: {
    title: string;
    timings?: string;
    rituals?: string[];
    events?: string[];
    description?: string;
  };
};

export type LostFoundWidget = {
  type: 'lost_and_found';
  data: {
    incidentType: 'PERSON' | 'ITEM';
    status: string;
    referenceId?: string;
    nextAction?: string;
  };
};

export type SOSWidget = {
  type: 'sos';
  data: {
    status: 'CONFIRMATION_REQUIRED' | 'PROCESSING' | 'ACTIVATED' | 'FAILED';
    message: string;
    controlRoomStatus?: string;
    timestamp?: string;
  };
};

export type EscalationWidget = {
  type: 'human_escalation';
  data: {
    status: string;
    message: string;
    contactAvailable?: boolean;
  };
};

/* -------------------------------------------------------------------------- */
/* IVR                                                                         */
/* -------------------------------------------------------------------------- */

/** Where the caller is in the backend menu tree. Mirrors `IvrState` server-side. */
export type IVRMenuState = 'language' | 'menu' | 'sos_confirm' | 'speech' | 'ended';

/** The call's own lifecycle, which is separate from the menu position. */
export type IVRCallState =
  | 'idle'
  | 'dialing'
  | 'connecting'
  /** A request failed on the network and is being retried. */
  | 'reconnecting'
  | 'connected'
  /** Hold-to-talk is down, but the prompt is still playing. */
  | 'waiting'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'ended'
  | 'failed';

export type IVROption = {
  key: string;
  label: string;
};

/** One turn from `/api/ivr/session/*`, after camelCase conversion. */
export type IVRTurn = {
  sessionId: string;
  state: IVRMenuState;
  language: Language;
  prompt: string;
  /** MP3 of `prompt`. Null when the backend has no speech provider configured. */
  audioBase64: string | null;
  mediaType: string;
  options: IVROption[];
  widgets?: ToolWidget[];
  endsSession: boolean;
};

export type IVRPreset = {
  /** Shown on the card and dialled into the keypad. */
  number: string;
  label: string;
  description: string;
  /** Skips the language menu when the line is language-specific. */
  language?: Language;
  /** Emergency lines get a different treatment on screen. */
  emergency?: boolean;
  /**
   * Keys sent automatically once connected, to land the caller deeper in the
   * menu. Used so the emergency line reaches its confirmation prompt directly
   * instead of making someone in trouble navigate there.
   */
  autoKeys?: string[];
};

export type LocationState = {
  latitude: number | null;
  longitude: number | null;
  permission: 'unknown' | 'granted' | 'denied';
  /**
   * True when these are the Pandharpur temple coordinates standing in for a fix
   * we could not get. The map must not claim "you are here" over them.
   */
  isFallback?: boolean;
};

export type Copy = {
  greeting: string;
  greetingSub: string;
  placeholder: string;
  listening: string;
  understanding: string;
  checking: string;
  crowd: string;
  facility: string;
  route: string;
  temple: string;
  help: string;
  settings: string;
  map: string;
  send: string;
  speak: string;
  stop: string;
  viewMap: string;
  viewRoute: string;
  nearby: string;
  poorConnection: string;
  getStarted: string;
  chooseLanguage: string;
  continueLabel: string;
  welcomeDescription: string;
  noMessages: string;
  helpTitle: string;
  helpDescription: string;
  emergency: string;
  emergencyPrompt: string;
  confirmSOS: string;
  cancel: string;
  location: string;
  allowLocation: string;
  locationDenied: string;
  clearConversation: string;
  readAloud: string;
  voiceInput: string;
  language: string;
  about: string;
  liveMap: string;
  recenter: string;
  recent: string;
  updated: string;
};