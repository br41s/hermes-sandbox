import React from 'react';
import {AbsoluteFill, OffthreadVideo, Sequence, interpolate, staticFile, useCurrentFrame} from 'remotion';
import type {FlowProps} from './types';
import {FONT, SAFE, palette, type Palette} from './theme';
import {Karaoke} from './components/Karaoke';
import {OutroCard} from './Tip';
import './fonts';

// The flow format (package "format": "flow"): one vertical clip the owner
// filmed in Google Flow, with his own voice. The Studio adds karaoke
// subtitles of what is heard, the small brand mark and the same animated
// outro card as every short. No cover-frames overlay at the start (unlike
// Short.tsx): the owner's face and first word ARE the opening.

const OutroFade: React.FC<{outroFrames: number; articleUrl: string; pal: Palette; lang: 'en' | 'es'; brand: {name: string; site: string}}> = ({outroFrames, articleUrl, pal, lang, brand}) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [0, 15], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  return (
    <AbsoluteFill style={{opacity, background: 'linear-gradient(165deg, #2A221B 0%, #17120D 70%, #2A221B 100%)'}}>
      <OutroCard beat={{kind: 'outro', from: 0, duration: outroFrames, number: 0, url: articleUrl, show: 'outro'}} pal={pal} lang={lang} brand={brand} />
    </AbsoluteFill>
  );
};

const Caption: React.FC<{text: string; position: 'top' | 'bottom'; pal: Palette; durationInFrames: number}> = ({text, position, pal, durationInFrames}) => {
  const frame = useCurrentFrame();
  const enter = interpolate(frame, [0, 8], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const exit = interpolate(frame, [durationInFrames - 6, durationInFrames], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const rise = interpolate(frame, [0, 8], [30, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const anchor = position === 'top' ? {top: SAFE.top + 110} : {bottom: 1920 - SAFE.bottom + 430};
  return (
    <div style={{position: 'absolute', left: SAFE.left, right: 1080 - SAFE.right + 20, ...anchor}}>
      <div style={{
        display: 'inline-block',
        maxWidth: '100%',
        background: pal.card,
        color: pal.text,
        fontFamily: FONT,
        fontWeight: 850,
        fontSize: text.length > 24 ? 52 : 62,
        lineHeight: 1.1,
        padding: '18px 28px',
        borderRadius: 18,
        borderLeft: `10px solid ${pal.accent}`,
        boxShadow: '0 10px 30px rgba(0,0,0,0.35)',
        opacity: enter * exit,
        transform: `translateY(${rise}px)`,
      }}>{text}</div>
    </div>
  );
};

export const Flow: React.FC<FlowProps> = (props) => {
  const pal = palette('brand');
  return (
    <AbsoluteFill style={{backgroundColor: '#000'}}>
      <Sequence from={0} durationInFrames={props.footageFrames} name="footage">
        <OffthreadVideo src={staticFile(props.footage)} muted style={{width: '100%', height: '100%', objectFit: 'cover'}} />
        <div style={{position: 'absolute', top: SAFE.top + 20, left: SAFE.left, opacity: 0.92, display: 'flex', flexDirection: 'row', alignItems: 'center', gap: 14}}>
          <div style={{width: 18, height: 18, background: pal.accent, transform: 'rotate(45deg)', borderRadius: 4}} />
          <div style={{fontFamily: FONT, fontWeight: 850, fontSize: 32, letterSpacing: 6, color: '#FFFFFF', textTransform: 'uppercase', textShadow: '0 3px 14px rgba(0,0,0,0.7)'}}>{props.brand.name}</div>
        </div>
      </Sequence>
      {props.overlays.map((o, i) => (
        <Sequence key={`${o.fromFrame}-${i}`} from={o.fromFrame} durationInFrames={o.durationInFrames} name="caption">
          <Caption text={o.text} position={o.position} pal={pal} durationInFrames={o.durationInFrames} />
        </Sequence>
      ))}
      <Karaoke words={props.words} pal={pal} />
      <Sequence from={props.outroFrom} durationInFrames={props.outroFrames} name="outro">
        <OutroFade outroFrames={props.outroFrames} articleUrl={props.articleUrl} pal={pal} lang={props.lang} brand={props.brand} />
      </Sequence>
    </AbsoluteFill>
  );
};
