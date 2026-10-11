// The props contract between plugins/shorts/studio/build.py and this template.
// build.py writes exactly this shape to props.json; keep the two in step.

export type BeatKind = 'opener' | 'hook' | 'point' | 'stat' | 'list' | 'quote' | 'compare' | 'avatar' | 'cta' | 'outro';

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
  /** Path under the public dir of the presenter clip (a hook or CTA said to camera). */
  presenter?: string | null;
  presenterName?: string;
  /** Tip format: the presenter full-frame, or an animated card with them in a bubble. */
  show?: 'presenter' | 'card' | 'outro';
  /** Tip format, on camera: first frame of this beat inside the presenter clip. */
  clipFrom?: number;
  /** compare beats (tip format): the before and the after. */
  before?: string;
  after?: string;
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
  /** 'tip': one tip, the presenter on camera between animated cards. */
  format?: 'classic' | 'tip';
  /** Tip format: the lip-synced clip of every on-camera line, or null when it was not made. */
  presenterClip?: string | null;
  /** Tip format: the presenter's loop recorded once, for the bubble; null uses their photo. */
  bubbleLoop?: string | null;
  bubbleLoopFrames?: number;
  /** Where the face is in the loop (the plain photo's framing), when the clip has another. */
  loopFaceY?: number;
  presenterName?: string;
  /** Where the presenter's face is, as a fraction of the frame height (framing of their photo). */
  faceY?: number;
  /** Path under the public dir of the presenter's photo, for the cover (tip format with a take). */
  presenterPhoto?: string | null;
  palette: string;
  motif: string;
  brand: {name: string; site: string};
  beats: Beat[];
  words: Word[];
  durationInFrames: number;
  cover: {title: string; kicker: string};
  thumb: {title: string};
};
