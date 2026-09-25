// The props contract between plugins/shorts/studio/build.py and this template.
// build.py writes exactly this shape to props.json; keep the two in step.

export type BeatKind = 'hook' | 'point' | 'stat' | 'list' | 'quote' | 'avatar' | 'cta';

export type Beat = {
  kind: BeatKind;
  /** First frame of the beat on the master timeline. */
  from: number;
  /** Length in frames — follows the measured voiceover. */
  duration: number;
  /** 1-based position among the middle beats (0 for hook and CTA). */
  number: number;
  onscreen?: string;
  kicker?: string;
  value?: string;
  label?: string;
  items?: string[];
  url?: string;
  /** Path under the public dir of this beat's B-roll, or null. */
  broll?: string | null;
  /** Path under the public dir of the avatar clip (avatar beats only). */
  avatar?: string | null;
  avatarName?: string;
};

export type Word = {
  text: string;
  startMs: number;
  endMs: number;
  /** Karaoke page: words sharing a page are on screen together. */
  page: number;
};

export type ShortProps = {
  lang: 'en' | 'es';
  palette: string;
  motif: string;
  brand: {name: string; site: string};
  beats: Beat[];
  words: Word[];
  durationInFrames: number;
  cover: {title: string; kicker: string};
  thumb: {title: string};
};
