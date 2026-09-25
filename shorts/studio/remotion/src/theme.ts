// Palettes and motifs. The names are mirrored in
// plugins/shorts/studio/package.py (PALETTES, MOTIFS) and a Python test asserts
// the two lists match — add a palette in both places or neither.

export type Palette = {
  bg: string;       // base background
  bg2: string;      // gradient partner
  accent: string;   // primary accent: highlights, progress, active word
  accent2: string;  // secondary accent: kickers, chips, motif
  text: string;
  muted: string;
  card: string;     // translucent card fill
};

export const PALETTES: Record<string, Palette> = {
  'indigo-coral': {bg: '#0B1220', bg2: '#1E1B4B', accent: '#818CF8', accent2: '#F97316', text: '#F8FAFC', muted: '#94A3B8', card: 'rgba(15,23,42,0.72)'},
  'teal-amber': {bg: '#071A1C', bg2: '#0F3B3A', accent: '#2DD4BF', accent2: '#F59E0B', text: '#F0FDFA', muted: '#99B4B0', card: 'rgba(4,30,32,0.72)'},
  'violet-lime': {bg: '#120B1F', bg2: '#2E1065', accent: '#A78BFA', accent2: '#A3E635', text: '#FAF5FF', muted: '#A99BBF', card: 'rgba(24,12,44,0.72)'},
  'cobalt-pink': {bg: '#0A1024', bg2: '#172554', accent: '#60A5FA', accent2: '#F472B6', text: '#F8FAFC', muted: '#93A4C3', card: 'rgba(10,20,50,0.72)'},
  'emerald-gold': {bg: '#06140F', bg2: '#064E3B', accent: '#34D399', accent2: '#FBBF24', text: '#F0FDF4', muted: '#94B8A8', card: 'rgba(3,30,22,0.72)'},
  'crimson-sky': {bg: '#1A0B0E', bg2: '#4C0519', accent: '#FB7185', accent2: '#38BDF8', text: '#FFF1F2', muted: '#C2A2A8', card: 'rgba(40,10,16,0.72)'},
  'slate-cyan': {bg: '#0F172A', bg2: '#1E293B', accent: '#22D3EE', accent2: '#F472B6', text: '#F8FAFC', muted: '#94A3B8', card: 'rgba(15,23,42,0.74)'},
  'orange-navy': {bg: '#0B1020', bg2: '#1E3A5F', accent: '#FB923C', accent2: '#60A5FA', text: '#FFF7ED', muted: '#A8B3C7', card: 'rgba(11,16,32,0.74)'},
};

export const MOTIFS = ['diamonds', 'circles', 'chevrons', 'dots', 'bars', 'rings', 'grid', 'waves'] as const;

export const palette = (name: string): Palette => PALETTES[name] ?? PALETTES['indigo-coral'];

// Safe areas for 1080x1920 vertical video. Instagram and YouTube draw their
// own UI over the top ~220px and bottom ~280px; nothing we need read goes there.
export const SAFE = {top: 230, bottom: 1920 - 290, left: 72, right: 1080 - 150};

export const FONT = 'Inter';
