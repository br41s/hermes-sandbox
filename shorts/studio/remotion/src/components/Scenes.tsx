import React from 'react';
import {AbsoluteFill, Easing, OffthreadVideo, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import type {Beat} from '../types';
import {FONT, SAFE, type Palette} from '../theme';

// Everything a scene draws lives between the top bar and the karaoke band.
const ZONE_TOP = SAFE.top + 150;
const ZONE_BOTTOM = SAFE.bottom - 400;

const useEnter = (delay = 0) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  return spring({frame: frame - delay, fps, config: {damping: 16, stiffness: 140, mass: 0.7}});
};

/** Fade out over the last frames of a scene so cuts never snap. */
const useExit = (duration: number, frames = 7) => {
  const frame = useCurrentFrame();
  return interpolate(frame, [duration - frames, duration], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
};

/** Smaller type for longer lines, so nothing needs a manual size per short. */
const fit = (text: string, base: number) => {
  const n = text.replace(/\*/g, '').length;
  if (n > 50) return Math.round(base * 0.72);
  if (n > 38) return Math.round(base * 0.82);
  if (n > 26) return Math.round(base * 0.92);
  return base;
};

/** `*word*` in on-screen text renders in the accent colour. */
const Emph: React.FC<{text: string; color: string}> = ({text, color}) => (
  <>
    {text.split(/(\*[^*]+\*)/g).map((part, i) =>
      part.startsWith('*') && part.endsWith('*') && part.length > 2 ? (
        <span key={i} style={{color}}>{part.slice(1, -1)}</span>
      ) : (
        <React.Fragment key={i}>{part}</React.Fragment>
      ),
    )}
  </>
);

const Zone: React.FC<{children: React.ReactNode; align?: 'center' | 'flex-start'; opacity?: number}> = ({children, align = 'center', opacity = 1}) => (
  <div
    style={{
      position: 'absolute',
      top: ZONE_TOP,
      bottom: 1920 - ZONE_BOTTOM,
      left: SAFE.left,
      right: 1080 - SAFE.right,
      display: 'flex',
      flexDirection: 'column',
      justifyContent: 'center',
      alignItems: align,
      opacity,
      fontFamily: FONT,
    }}
  >
    {children}
  </div>
);

const Headline: React.FC<{text: string; pal: Palette; base: number; delay?: number}> = ({text, pal, base, delay = 0}) => {
  const frame = useCurrentFrame();
  const words = text.split(/\s+/);
  const size = fit(text, base);
  return (
    <div style={{fontSize: size, fontWeight: 850, lineHeight: 1.04, color: pal.text, letterSpacing: -1.5, display: 'flex', flexWrap: 'wrap', gap: `0 ${size * 0.26}px`}}>
      {words.map((w, i) => {
        const p = interpolate(frame - delay - i * 3, [0, 9], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic)});
        return (
          <span key={i} style={{opacity: p, transform: `translateY(${(1 - p) * 38}px)`, display: 'inline-block'}}>
            <Emph text={w} color={pal.accent} />
          </span>
        );
      })}
    </div>
  );
};

const Chip: React.FC<{label: string; pal: Palette; bg?: string; color?: string}> = ({label, pal, bg, color}) => (
  <div
    style={{
      display: 'inline-flex',
      alignItems: 'center',
      padding: '10px 22px',
      borderRadius: 999,
      background: bg ?? pal.accent2,
      color: color ?? pal.bg,
      fontWeight: 800,
      fontSize: 32,
      letterSpacing: 2,
      textTransform: 'uppercase',
      marginBottom: 34,
    }}
  >
    {label}
  </div>
);

export const HookScene: React.FC<{beat: Beat; pal: Palette}> = ({beat, pal}) => {
  const enter = useEnter();
  const exit = useExit(beat.duration);
  const frame = useCurrentFrame();
  const sweep = interpolate(frame, [10, 28], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.inOut(Easing.cubic)});
  return (
    <Zone align="flex-start" opacity={exit}>
      {beat.kicker ? (
        <div style={{transform: `scale(${enter})`, transformOrigin: 'left center'}}>
          <Chip label={beat.kicker} pal={pal} />
        </div>
      ) : null}
      <Headline text={beat.onscreen ?? ''} pal={pal} base={112} delay={3} />
      <div style={{marginTop: 36, height: 12, width: 360 * sweep, background: pal.accent, borderRadius: 6}} />
    </Zone>
  );
};

export const PointScene: React.FC<{beat: Beat; pal: Palette}> = ({beat, pal}) => {
  const enter = useEnter();
  const exit = useExit(beat.duration);
  return (
    <Zone align="flex-start" opacity={exit}>
      <div
        style={{
          transform: `translateY(${(1 - enter) * 60}px)`,
          opacity: enter,
          background: pal.card,
          borderLeft: `10px solid ${pal.accent}`,
          borderRadius: 28,
          padding: '48px 50px 54px',
          width: '100%',
          boxShadow: '0 30px 80px rgba(0,0,0,0.45)',
        }}
      >
        <div style={{fontSize: 44, fontWeight: 900, color: pal.accent2, marginBottom: 20, letterSpacing: 2}}>
          {String(beat.number).padStart(2, '0')}
        </div>
        <Headline text={beat.onscreen ?? ''} pal={pal} base={84} delay={4} />
      </div>
    </Zone>
  );
};

const countUp = (value: string, p: number): string => {
  const m = value.match(/^([^\d]*)([\d][\d.,]*)(.*)$/);
  if (!m) return value;
  const [, pre, num, post] = m;
  const thousands = /^\d{1,3}([.,]\d{3})+$/.test(num);
  const sep = thousands ? num.replace(/\d/g, '')[0] : '';
  const decimals = !thousands && /[.,]/.test(num) ? num.split(/[.,]/)[1].length : 0;
  const n = parseFloat(thousands ? num.replace(/[.,]/g, '') : num.replace(',', '.'));
  // Years do not count up — "0 → 2026" reads as a glitch, not a stat.
  if (!isFinite(n) || (!pre && !post && n >= 1900 && n <= 2100)) return value;
  const cur = n * p;
  let s = decimals ? cur.toFixed(decimals) : Math.round(cur).toString();
  if (sep) s = s.replace(/\B(?=(\d{3})+(?!\d))/g, sep);
  if (!thousands && decimals && num.includes(',')) s = s.replace('.', ',');
  return `${pre}${s}${post}`;
};

export const StatScene: React.FC<{beat: Beat; pal: Palette}> = ({beat, pal}) => {
  const frame = useCurrentFrame();
  const enter = useEnter();
  const exit = useExit(beat.duration);
  const p = interpolate(frame, [4, 34], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic)});
  const value = beat.value ?? '';
  const size = value.length > 6 ? 190 : value.length > 4 ? 230 : 270;
  return (
    <Zone opacity={exit}>
      <div style={{fontSize: size, fontWeight: 900, color: pal.accent, letterSpacing: -6, lineHeight: 1, transform: `scale(${0.85 + enter * 0.15})`, textShadow: '0 20px 60px rgba(0,0,0,0.5)'}}>
        {countUp(value, p)}
      </div>
      <div style={{marginTop: 34, maxWidth: 860, textAlign: 'center', fontSize: fit(beat.label ?? '', 64), fontWeight: 750, color: pal.text, lineHeight: 1.12, opacity: enter}}>
        <Emph text={beat.label ?? ''} color={pal.accent2} />
      </div>
    </Zone>
  );
};

export const ListScene: React.FC<{beat: Beat; pal: Palette}> = ({beat, pal}) => {
  const frame = useCurrentFrame();
  const exit = useExit(beat.duration);
  const items = beat.items ?? [];
  // Spread the reveals across the first 70% of the beat, roughly as they are spoken.
  const step = Math.max(8, Math.floor((beat.duration * 0.7) / Math.max(items.length, 1)));
  return (
    <Zone align="flex-start" opacity={exit}>
      <Headline text={beat.onscreen ?? ''} pal={pal} base={80} />
      <div style={{marginTop: 44, display: 'flex', flexDirection: 'column', gap: 26, width: '100%'}}>
        {items.map((item, i) => {
          const p = interpolate(frame - 8 - i * step, [0, 10], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic)});
          return (
            <div key={i} style={{display: 'flex', alignItems: 'center', gap: 26, opacity: p, transform: `translateX(${(1 - p) * -50}px)`, background: pal.card, borderRadius: 22, padding: '24px 30px'}}>
              <div style={{width: 26, height: 26, flex: 'none', background: i % 2 ? pal.accent2 : pal.accent, transform: 'rotate(45deg)', borderRadius: 4}} />
              <div style={{fontSize: 56, fontWeight: 780, color: pal.text, lineHeight: 1.1}}>{item}</div>
            </div>
          );
        })}
      </div>
    </Zone>
  );
};

export const QuoteScene: React.FC<{beat: Beat; pal: Palette}> = ({beat, pal}) => {
  const enter = useEnter();
  const exit = useExit(beat.duration);
  return (
    <Zone align="flex-start" opacity={exit}>
      <div style={{fontSize: 260, fontWeight: 900, color: pal.accent2, lineHeight: 0.6, height: 130, opacity: enter}}>“</div>
      <div style={{fontStyle: 'italic'}}>
        <Headline text={beat.onscreen ?? ''} pal={pal} base={82} delay={4} />
      </div>
    </Zone>
  );
};

export const AvatarScene: React.FC<{beat: Beat; pal: Palette}> = ({beat, pal}) => {
  const frame = useCurrentFrame();
  const enter = useEnter(4);
  const fade = interpolate(frame, [0, 6, beat.duration - 6, beat.duration], [0, 1, 1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  return (
    <AbsoluteFill style={{opacity: fade}}>
      {beat.avatar ? (
        <OffthreadVideo src={staticFile(beat.avatar)} muted style={{width: '100%', height: '100%', objectFit: 'cover'}} />
      ) : null}
      <AbsoluteFill style={{background: `linear-gradient(180deg, ${pal.bg}B3 0%, transparent 22%, transparent 62%, ${pal.bg}E6 100%)`}} />
      {beat.avatarName ? (
        <div style={{position: 'absolute', left: SAFE.left, top: SAFE.top + 120, transform: `translateX(${(1 - enter) * -80}px)`, opacity: enter, fontFamily: FONT}}>
          <div style={{display: 'inline-block', background: pal.accent, color: pal.bg, fontWeight: 850, fontSize: 40, padding: '10px 24px', borderRadius: 14}}>{beat.avatarName}</div>
        </div>
      ) : null}
    </AbsoluteFill>
  );
};

export const CtaScene: React.FC<{beat: Beat; pal: Palette; brand: string}> = ({beat, pal, brand}) => {
  const frame = useCurrentFrame();
  const enter = useEnter();
  const pulse = 1 + Math.sin(frame / 7) * 0.025;
  // The link is not clickable in a video: show the domain big, the path small.
  const bare = (beat.url ?? '').replace(/^https?:\/\//, '').replace(/\/$/, '');
  const slash = bare.indexOf('/');
  const host = slash < 0 ? bare : bare.slice(0, slash);
  const path = slash < 0 ? '' : bare.slice(slash);
  return (
    <Zone>
      <div style={{opacity: enter, transform: `translateY(${(1 - enter) * 40}px)`, textAlign: 'center', display: 'flex', flexDirection: 'column', alignItems: 'center'}}>
        <Headline text={beat.onscreen ?? ''} pal={pal} base={92} />
        <div
          style={{
            marginTop: 54,
            transform: `scale(${pulse})`,
            background: pal.accent,
            color: pal.bg,
            borderRadius: 999,
            padding: '26px 52px',
            fontSize: 54,
            fontWeight: 900,
            boxShadow: `0 20px 60px ${pal.accent}55`,
          }}
        >
          {host || brand} →
        </div>
        {path && path.length <= 48 ? (
          <div style={{marginTop: 30, fontSize: 34, fontWeight: 700, color: pal.muted}}>{path}</div>
        ) : null}
      </div>
    </Zone>
  );
};
