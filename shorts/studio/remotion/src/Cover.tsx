import React from 'react';
import {AbsoluteFill} from 'remotion';
import type {ShortProps} from './types';
import {FONT, SAFE, palette} from './theme';
import {MotifField} from './components/Background';
import './fonts';

// Titled covers. A short without one does not get published (checklist rule),
// so these are compositions of their own rather than a grabbed frame.

const TitleBlock: React.FC<{title: string; kicker?: string; pal: ReturnType<typeof palette>; size: number}> = ({title, kicker, pal, size}) => {
  const words = title.split(/\s+/);
  const lastIdx = words.length - 1;
  return (
    <div style={{position: 'absolute', left: SAFE.left, right: 1080 - SAFE.right + 20, top: 560, fontFamily: FONT}}>
      {kicker ? (
        <div style={{display: 'inline-block', background: pal.accent2, color: pal.bg, fontWeight: 850, fontSize: 36, letterSpacing: 3, textTransform: 'uppercase', padding: '12px 26px', borderRadius: 999, marginBottom: 44}}>
          {kicker}
        </div>
      ) : null}
      <div style={{fontSize: size, fontWeight: 900, lineHeight: 1.0, letterSpacing: -3, color: pal.text}}>
        {words.map((w, i) => (
          <span key={i} style={{color: i === lastIdx ? pal.accent : pal.text}}>{w}{i < lastIdx ? ' ' : ''}</span>
        ))}
      </div>
      <div style={{marginTop: 50, height: 16, width: 300, background: pal.accent, borderRadius: 8}} />
    </div>
  );
};

const Frame: React.FC<{props: ShortProps; title: string; kicker?: string}> = ({props, title, kicker}) => {
  const pal = palette(props.palette);
  const n = title.length;
  const size = n > 40 ? 104 : n > 28 ? 122 : 140;
  return (
    <AbsoluteFill style={{background: `linear-gradient(165deg, ${pal.bg} 0%, ${pal.bg2} 60%, ${pal.bg} 100%)`}}>
      <MotifField motif={props.motif} pal={pal} seed={`cover-${props.palette}`} intensity={1.6} />
      <div style={{position: 'absolute', top: SAFE.top + 20, left: SAFE.left, display: 'flex', alignItems: 'center', gap: 16, fontFamily: FONT}}>
        <div style={{width: 22, height: 22, background: pal.accent2, transform: 'rotate(45deg)', borderRadius: 4}} />
        <div style={{fontWeight: 850, fontSize: 40, letterSpacing: 7, color: pal.text, textTransform: 'uppercase'}}>{props.brand.name}</div>
      </div>
      <TitleBlock title={title} kicker={kicker} pal={pal} size={size} />
      <div style={{position: 'absolute', bottom: 1920 - SAFE.bottom + 60, left: SAFE.left, fontFamily: FONT, fontWeight: 750, fontSize: 38, color: pal.muted}}>
        {props.brand.site}
      </div>
    </AbsoluteFill>
  );
};

export const Cover: React.FC<ShortProps> = (props) => <Frame props={props} title={props.cover.title} kicker={props.cover.kicker} />;
export const Thumb: React.FC<ShortProps> = (props) => <Frame props={props} title={props.thumb.title} />;
