import React from 'react';
import {AbsoluteFill, Easing, OffthreadVideo, Sequence, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import type {Beat, ShortProps} from './types';
import {FONT, SAFE, palette, type Palette} from './theme';
import {MotifField} from './components/Background';
import {TopBar} from './components/TopBar';
import {Karaoke} from './components/Karaoke';
import {CtaScene, HookScene} from './components/Scenes';
import './fonts';

// The tip format (package.py, "format": "tip"). One take of the presenter
// runs under the whole short, so one voice and one face carry it; each beat
// only decides how it is seen: full-frame ("presenter", with a cut-in zoom
// like an edit) or shrunk into a bubble while an animated card shows the tip
// ("card"). Without a take the same cards run over the brand background,
// voiced by Edge.

const W = 1080;
const H = 1920;
const CARD_TOP = SAFE.top + 150;
const CARD_BOTTOM = 1000;
const BUBBLE = {size: 300, right: 56, top: 1010};
const BUBBLE_VIDEO = 0.55;     // the take's scale inside the bubble
const MORPH = 10;              // frames to shrink into / grow out of the bubble
const ZOOMS = [1.0, 1.16, 1.07];

const COPY = {
  es: {before: 'Antes', after: 'Ahora', save: 'Guárdalo', share: 'Compártelo'},
  en: {before: 'Before', after: 'Now', save: 'Save it', share: 'Share it'},
};

const ease = Easing.bezier(0.33, 0, 0.2, 1);

const useEnter = (delay = 0) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  return spring({frame: frame - delay, fps, config: {damping: 15, stiffness: 150, mass: 0.7}});
};

const fit = (text: string, base: number) => {
  const n = text.replace(/\*/g, '').length;
  if (n > 50) return Math.round(base * 0.7);
  if (n > 38) return Math.round(base * 0.8);
  if (n > 26) return Math.round(base * 0.9);
  return base;
};

/** `*word*` is the key word: accent colour and a highlighter that sweeps in behind it. */
const KeyText: React.FC<{text: string; pal: Palette; size: number; delay?: number; sweepAt?: number}> = ({text, pal, size, delay = 0, sweepAt = 12}) => {
  const frame = useCurrentFrame();
  const words = text.split(/\s+/);
  return (
    <div style={{fontSize: size, fontWeight: 850, lineHeight: 1.08, letterSpacing: -1.5, color: pal.text, display: 'flex', flexWrap: 'wrap', gap: `${size * 0.08}px ${size * 0.24}px`}}>
      {words.map((w, i) => {
        const p = interpolate(frame - delay - i * 2.5, [0, 9], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: ease});
        const key = /^\*.+\*[.,!?:;]?$/.test(w);
        const clean = w.replace(/\*/g, '');
        const sweep = interpolate(frame - delay - sweepAt, [0, 10], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: ease});
        return (
          <span key={i} style={{position: 'relative', display: 'inline-block', opacity: p, transform: `translateY(${(1 - p) * 30}px)`}}>
            {key ? (
              <span style={{position: 'absolute', left: -8, right: -8, bottom: size * 0.04, height: size * 0.42, background: pal.accent2, opacity: 0.85, borderRadius: 8, transformOrigin: 'left', transform: `scaleX(${sweep})`}} />
            ) : null}
            <span style={{position: 'relative'}}>{clean}</span>
          </span>
        );
      })}
    </div>
  );
};

const Panel: React.FC<{pal: Palette; children: React.ReactNode; style?: React.CSSProperties}> = ({pal, children, style}) => (
  <div style={{background: pal.card, border: `2px solid ${pal.text}1A`, borderRadius: 34, padding: '40px 44px', boxShadow: '0 30px 80px rgba(0,0,0,0.35)', ...style}}>{children}</div>
);

const CardZone: React.FC<{children: React.ReactNode}> = ({children}) => (
  <div style={{position: 'absolute', top: CARD_TOP, bottom: H - CARD_BOTTOM, left: SAFE.left, right: W - SAFE.right, display: 'flex', flexDirection: 'column', justifyContent: 'center', fontFamily: FONT}}>
    {children}
  </div>
);

// ---------------------------------------------------------------------------
// Cards

const KeyLineCard: React.FC<{beat: Beat; pal: Palette}> = ({beat, pal}) => {
  const enter = useEnter(2);
  const quote = beat.kind === 'quote';
  return (
    <CardZone>
      <Panel pal={pal} style={{transform: `translateY(${(1 - enter) * 60}px) scale(${0.96 + enter * 0.04})`, opacity: enter}}>
        {quote ? <div style={{fontSize: 150, lineHeight: 0.6, fontWeight: 900, color: pal.accent, marginBottom: 10}}>“</div> : null}
        <KeyText text={beat.onscreen ?? ''} pal={pal} size={fit(beat.onscreen ?? '', 92)} delay={5} sweepAt={Math.min(30, Math.round(beat.duration * 0.25))} />
      </Panel>
    </CardZone>
  );
};

const parseValue = (value: string) => {
  const m = value.match(/^(\D*?)(\d+(?:[.,]\d+)?)(.*)$/);
  if (!m) return null;
  const num = parseFloat(m[2].replace(',', '.'));
  const decimals = (m[2].split(/[.,]/)[1] ?? '').length;
  return {prefix: m[1], num, decimals, sep: m[2].includes(',') ? ',' : '.', suffix: m[3]};
};

const StatCard: React.FC<{beat: Beat; pal: Palette}> = ({beat, pal}) => {
  const frame = useCurrentFrame();
  const enter = useEnter(2);
  const value = beat.value ?? '';
  const parsed = parseValue(value);
  const count = interpolate(frame, [6, 6 + Math.min(36, beat.duration * 0.45)], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic)});
  const shown = parsed ? `${parsed.prefix}${(parsed.num * count).toFixed(parsed.decimals).replace('.', parsed.sep)}${parsed.suffix}` : value;
  const pct = parsed && /%/.test(parsed.suffix) ? Math.min(parsed.num, 100) / 100 : null;
  const R = 230;
  const C = 2 * Math.PI * R;
  return (
    <CardZone>
      <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center', opacity: enter, transform: `scale(${0.9 + enter * 0.1})`}}>
        <div style={{position: 'relative', width: 2 * R + 40, height: 2 * R + 40, display: 'flex', alignItems: 'center', justifyContent: 'center'}}>
          <svg width={2 * R + 40} height={2 * R + 40} style={{position: 'absolute', inset: 0, transform: 'rotate(-90deg)'}}>
            <circle cx={R + 20} cy={R + 20} r={R} fill="none" stroke={`${pal.text}1F`} strokeWidth={26} />
            <circle cx={R + 20} cy={R + 20} r={R} fill="none" stroke={pal.accent} strokeWidth={26} strokeLinecap="round"
              strokeDasharray={C} strokeDashoffset={C * (1 - (pct ?? 1) * count)} />
          </svg>
          <div style={{fontSize: shown.length > 6 ? 130 : 170, fontWeight: 900, color: pal.text, letterSpacing: -4}}>{shown}</div>
        </div>
        <div style={{marginTop: 34, display: 'flex', justifyContent: 'center'}}>
          <KeyText text={beat.label ?? ''} pal={pal} size={fit(beat.label ?? '', 56)} delay={18} sweepAt={30} />
        </div>
      </div>
    </CardZone>
  );
};

const StepsCard: React.FC<{beat: Beat; pal: Palette}> = ({beat, pal}) => {
  const frame = useCurrentFrame();
  const items = beat.items ?? [];
  const span = beat.duration * 0.72;
  const at = (i: number) => 6 + (i * span) / Math.max(items.length, 1);
  const title = useEnter(0);
  return (
    <CardZone>
      {beat.onscreen ? (
        <div style={{fontSize: fit(beat.onscreen, 70), fontWeight: 850, color: pal.text, marginBottom: 38, lineHeight: 1.1, opacity: title}}>{beat.onscreen.replace(/\*/g, '')}</div>
      ) : null}
      <div style={{position: 'relative'}}>
        {items.map((item, i) => {
          const p = spring({frame: frame - at(i), fps: 30, config: {damping: 14, stiffness: 160, mass: 0.6}});
          const done = frame > at(i + 1) - 4 || (i === items.length - 1 && frame > at(i) + 18);
          return (
            <div key={i} style={{display: 'flex', alignItems: 'center', gap: 28, marginBottom: 26, opacity: p, transform: `translateX(${(1 - p) * -70}px)`}}>
              <div style={{width: 96, height: 96, flex: '0 0 96px', borderRadius: 26, background: done ? pal.accent : 'transparent', border: `5px solid ${pal.accent}`, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 46, fontWeight: 900, color: done ? pal.bg : pal.accent}}>
                {done ? '✓' : i + 1}
              </div>
              <div style={{fontSize: 66, fontWeight: 850, color: pal.text, lineHeight: 1.08}}>{item}</div>
            </div>
          );
        })}
      </div>
    </CardZone>
  );
};

const CompareCard: React.FC<{beat: Beat; pal: Palette; lang: 'en' | 'es'}> = ({beat, pal, lang}) => {
  const frame = useCurrentFrame();
  const copy = COPY[lang];
  const first = useEnter(2);
  const second = spring({frame: frame - Math.round(beat.duration * 0.42), fps: 30, config: {damping: 14, stiffness: 150, mass: 0.7}});
  const strike = interpolate(frame - Math.round(beat.duration * 0.42), [0, 10], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const row = (label: string, text: string, good: boolean, p: number) => (
    <div style={{opacity: p, transform: `translateY(${(1 - p) * 50}px)`, background: good ? `${pal.accent}22` : pal.card, border: `3px solid ${good ? pal.accent : `${pal.text}22`}`, borderRadius: 30, padding: '30px 36px', marginBottom: 26}}>
      <div style={{display: 'flex', alignItems: 'center', gap: 16, marginBottom: 12}}>
        <div style={{width: 52, height: 52, borderRadius: 26, background: good ? pal.accent : `${pal.text}33`, color: good ? pal.bg : pal.text, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 34, fontWeight: 900}}>{good ? '✓' : '✕'}</div>
        <div style={{fontSize: 38, fontWeight: 850, letterSpacing: 3, textTransform: 'uppercase', color: good ? pal.accent : pal.muted}}>{label}</div>
      </div>
      <div style={{position: 'relative', fontSize: fit(text, 70), fontWeight: 850, color: good ? pal.text : pal.muted, lineHeight: 1.12}}>
        {text}
        {!good ? <div style={{position: 'absolute', left: 0, top: '52%', height: 6, width: `${strike * 100}%`, background: pal.accent2, borderRadius: 3}} /> : null}
      </div>
    </div>
  );
  return (
    <CardZone>
      {beat.onscreen ? <div style={{fontSize: fit(beat.onscreen, 70), fontWeight: 850, color: pal.text, marginBottom: 34, opacity: first}}>{beat.onscreen.replace(/\*/g, '')}</div> : null}
      {row(copy.before, beat.before ?? '', false, first)}
      {row(copy.after, beat.after ?? '', true, second)}
    </CardZone>
  );
};

const Card: React.FC<{beat: Beat; pal: Palette; lang: 'en' | 'es'}> = ({beat, pal, lang}) => {
  switch (beat.kind) {
    case 'stat': return <StatCard beat={beat} pal={pal} />;
    case 'list': return <StepsCard beat={beat} pal={pal} />;
    case 'compare': return <CompareCard beat={beat} pal={pal} lang={lang} />;
    default: return <KeyLineCard beat={beat} pal={pal} />;
  }
};

// ---------------------------------------------------------------------------
// What sits over the presenter while they are full-frame

const overTop = (faceY: number) => Math.max(800, Math.round(faceY * H + 330));

const HookOverlay: React.FC<{beat: Beat; pal: Palette; faceY: number}> = ({beat, pal, faceY}) => {
  const enter = useEnter(3);
  return (
    <div style={{position: 'absolute', top: overTop(faceY), left: SAFE.left, right: W - SAFE.right, fontFamily: FONT, opacity: enter, transform: `translateY(${(1 - enter) * 40}px)`}}>
      {beat.kicker ? (
        <div style={{display: 'inline-block', background: pal.accent2, color: pal.bg, fontWeight: 850, fontSize: 32, padding: '6px 20px', borderRadius: 12, marginBottom: 16, letterSpacing: 1}}>{beat.kicker}</div>
      ) : null}
      <div style={{background: `${pal.bg}D9`, borderRadius: 28, padding: '26px 34px 30px'}}>
        <KeyText text={beat.onscreen ?? ''} pal={pal} size={fit(beat.onscreen ?? '', 72)} delay={4} sweepAt={14} />
      </div>
    </div>
  );
};

const LowerThird: React.FC<{beat: Beat; pal: Palette; faceY: number}> = ({beat, pal, faceY}) => {
  const enter = useEnter(4);
  if (!beat.onscreen) return null;
  return (
    <div style={{position: 'absolute', top: overTop(faceY) + 60, left: SAFE.left, right: W - SAFE.right, fontFamily: FONT, opacity: enter, transform: `translateX(${(1 - enter) * -60}px)`}}>
      <div style={{display: 'inline-block', background: `${pal.bg}D9`, borderLeft: `10px solid ${pal.accent}`, borderRadius: 20, padding: '20px 30px'}}>
        <KeyText text={beat.onscreen} pal={pal} size={fit(beat.onscreen, 60)} delay={4} sweepAt={12} />
      </div>
    </div>
  );
};

const Icon: React.FC<{kind: 'save' | 'share'; color: string}> = ({kind, color}) => (
  <svg width="52" height="52" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
    {kind === 'save' ? <path d="M6 3h12v18l-6-4-6 4z" /> : (
      <>
        <circle cx="18" cy="5" r="3" /><circle cx="6" cy="12" r="3" /><circle cx="18" cy="19" r="3" />
        <path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4" />
      </>
    )}
  </svg>
);

/** The CTA: save it, share it, where the full guide is. */
const SaveShareOverlay: React.FC<{beat: Beat; pal: Palette; faceY: number; lang: 'en' | 'es'; brand: string}> = ({beat, pal, faceY, lang, brand}) => {
  const frame = useCurrentFrame();
  const copy = COPY[lang];
  const enter = useEnter(3);
  const chips: Array<['save' | 'share', string]> = [['save', copy.save], ['share', copy.share]];
  const bare = (beat.url ?? '').replace(/^https?:\/\//, '').replace(/\/$/, '');
  const host = bare.split('/')[0] || brand;
  return (
    <div style={{position: 'absolute', top: overTop(faceY), left: SAFE.left, right: W - SAFE.right, fontFamily: FONT, opacity: enter}}>
      <div style={{background: `${pal.bg}E0`, borderRadius: 30, padding: '28px 34px 32px', transform: `translateY(${(1 - enter) * 40}px)`}}>
        <KeyText text={beat.onscreen ?? ''} pal={pal} size={fit(beat.onscreen ?? '', 62)} delay={4} sweepAt={14} />
        <div style={{display: 'flex', gap: 18, marginTop: 26}}>
          {chips.map(([kind, label], i) => {
            const p = spring({frame: frame - 10 - i * 6, fps: 30, config: {damping: 10, stiffness: 180, mass: 0.6}});
            return (
              <div key={kind} style={{display: 'flex', alignItems: 'center', gap: 12, background: i === 0 ? pal.accent : pal.accent2, color: pal.bg, borderRadius: 999, padding: '14px 28px 14px 20px', fontSize: 40, fontWeight: 900, transform: `scale(${p})`}}>
                <Icon kind={kind} color={pal.bg} />{label}
              </div>
            );
          })}
        </div>
        <div style={{marginTop: 22, fontSize: 38, fontWeight: 800, color: pal.muted}}>{host} →</div>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// The take: one video under the whole short, full-frame or in its bubble

const TakeLayer: React.FC<{src: string; beats: Beat[]; pal: Palette; faceY: number; name?: string}> = ({src, beats, pal, faceY, name}) => {
  const frame = useCurrentFrame();
  let idx = beats.findIndex((b) => frame >= b.from && frame < b.from + b.duration);
  if (idx < 0) idx = beats.length - 1;
  const beat = beats[idx];
  const full = (b?: Beat) => (b?.show === 'card' ? 0 : 1);
  const prev = idx > 0 ? full(beats[idx - 1]) : full(beat);
  const w = interpolate(frame - beat.from, [0, MORPH], [prev, full(beat)], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: ease});

  // Each full-frame beat gets its own framing, like a cut between two cameras, and drifts in slowly.
  const nth = beats.slice(0, idx + 1).filter((b) => b.show !== 'card').length - 1;
  const zoom = ZOOMS[Math.max(nth, 0) % ZOOMS.length] + 0.035 * Math.min(1, (frame - beat.from) / Math.max(beat.duration, 1));

  const bx = W - BUBBLE.right - BUBBLE.size;
  const box = {
    left: interpolate(w, [0, 1], [bx, 0]),
    top: interpolate(w, [0, 1], [BUBBLE.top, 0]),
    width: interpolate(w, [0, 1], [BUBBLE.size, W]),
    height: interpolate(w, [0, 1], [BUBBLE.size, H]),
    radius: interpolate(w, [0, 1], [BUBBLE.size / 2, 0]),
  };
  const vw = W * BUBBLE_VIDEO;
  const vh = H * BUBBLE_VIDEO;
  const video = {
    left: interpolate(w, [0, 1], [BUBBLE.size / 2 - vw / 2, 0]),
    top: interpolate(w, [0, 1], [BUBBLE.size / 2 - faceY * vh, 0]),
    width: interpolate(w, [0, 1], [vw, W]),
    height: interpolate(w, [0, 1], [vh, H]),
  };
  return (
    <>
      <div style={{position: 'absolute', left: box.left, top: box.top, width: box.width, height: box.height, borderRadius: box.radius, overflow: 'hidden',
                   boxShadow: w < 1 ? `0 0 0 ${8 * (1 - w)}px ${pal.accent}, 0 24px 60px rgba(0,0,0,${0.45 * (1 - w)})` : 'none'}}>
        <div style={{position: 'absolute', left: video.left, top: video.top, width: video.width, height: video.height,
                     transform: `scale(${1 + (zoom - 1) * w})`, transformOrigin: `50% ${faceY * 100}%`}}>
          <OffthreadVideo src={staticFile(src)} muted style={{width: '100%', height: '100%', objectFit: 'cover'}} />
        </div>
        {w > 0.5 ? (
          <AbsoluteFill style={{background: `linear-gradient(180deg, ${pal.bg}99 0%, transparent 16%, transparent 46%, ${pal.bg}40 62%, ${pal.bg}D9 100%)`, opacity: (w - 0.5) * 2}} />
        ) : null}
      </div>
      {name && w < 0.05 ? (
        <div style={{position: 'absolute', left: bx - 10, top: BUBBLE.top + BUBBLE.size - 30, transform: 'translateX(-100%)', fontFamily: FONT,
                     background: `${pal.bg}D9`, color: pal.text, fontWeight: 800, fontSize: 30, padding: '6px 16px', borderRadius: 10, borderRight: `5px solid ${pal.accent}`}}>
          {name}
        </div>
      ) : null}
    </>
  );
};

const Backdrop: React.FC<{pal: Palette; motif: string; seed: string}> = ({pal, motif, seed}) => (
  <AbsoluteFill style={{background: `linear-gradient(165deg, ${pal.bg} 0%, ${pal.bg2} 55%, ${pal.bg} 100%)`}}>
    <MotifField motif={motif} pal={pal} seed={seed} intensity={0.9} />
  </AbsoluteFill>
);

export const TipShort: React.FC<ShortProps> = (props) => {
  const pal = palette(props.palette);
  const seed = `${props.palette}-${props.motif}-tip`;
  const faceY = props.faceY ?? 0.25;
  const take = props.take ?? null;
  const lang = props.lang;
  return (
    <AbsoluteFill style={{backgroundColor: pal.bg}}>
      <Backdrop pal={pal} motif={props.motif} seed={seed} />
      {props.beats.map((beat, i) => {
        // Without the take nobody is on camera: every middle beat is a card,
        // and the hook and CTA fall back to the classic template scenes.
        const card = beat.show === 'card' || (!take && beat.kind !== 'hook' && beat.kind !== 'cta');
        if (!card && take) return null;
        return (
          <Sequence key={`c${i}`} from={beat.from} durationInFrames={beat.duration} name={`${i}-${beat.kind}-card`}>
            {card ? <Card beat={beat} pal={pal} lang={lang} /> : beat.kind === 'cta' ? <CtaScene beat={beat} pal={pal} brand={props.brand.site} /> : <HookScene beat={beat} pal={pal} />}
          </Sequence>
        );
      })}
      {take ? <TakeLayer src={take} beats={props.beats} pal={pal} faceY={faceY} name={props.presenterName} /> : null}
      {take ? props.beats.map((beat, i) => beat.show === 'card' ? null : (
        <Sequence key={`o${i}`} from={beat.from} durationInFrames={beat.duration} name={`${i}-${beat.kind}-over`}>
          {beat.kind === 'hook' ? <HookOverlay beat={beat} pal={pal} faceY={faceY} />
            : beat.kind === 'cta' ? <SaveShareOverlay beat={beat} pal={pal} faceY={faceY} lang={lang} brand={props.brand.site} />
            : <LowerThird beat={beat} pal={pal} faceY={faceY} />}
        </Sequence>
      )) : null}
      <TopBar beats={props.beats} pal={pal} brand={props.brand.name} />
      <Karaoke words={props.words} pal={pal} />
    </AbsoluteFill>
  );
};
