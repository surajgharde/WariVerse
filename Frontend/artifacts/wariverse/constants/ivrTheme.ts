/**
 * Palette for the dialer and call screen.
 *
 * Deliberately its own dark theme rather than `useColors()`: the rest of the
 * app is a warm cream light palette, but a phone call screen is dark on every
 * platform people already know, and it needs to stay dark whatever the device
 * appearance setting says. Accents are the brief's teal / amber / red.
 */
export const ivrTheme = {
  background: '#0b1220',
  surface: '#151d2e',
  surfaceRaised: '#1e2739',
  border: '#26314a',

  text: '#f2f6fb',
  textMuted: '#8ea0bb',
  textFaint: '#5c6b85',

  teal: '#0d9488',
  tealGlow: 'rgba(13, 148, 136, 0.28)',
  amber: '#d97706',
  amberGlow: 'rgba(217, 119, 6, 0.24)',
  red: '#dc2626',
  redGlow: 'rgba(220, 38, 38, 0.28)',
  green: '#16a34a',
} as const;

export type IvrTheme = typeof ivrTheme;
