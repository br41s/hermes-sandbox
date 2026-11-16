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
      <Karaoke words={props.words} pal={pal} />
      <Sequence from={props.outroFrom} durationInFrames={props.outroFrames} name="outro">
        <OutroFade outroFrames={props.outroFrames} articleUrl={props.articleUrl} pal={pal} lang={props.lang} brand={props.brand} />
      </Sequence>
    </AbsoluteFill>
  );
};
