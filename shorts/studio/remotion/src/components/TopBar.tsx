import React from 'react';
import {interpolate, useCurrentFrame} from 'remotion';
import type {Beat} from '../types';
import {FONT, SAFE, onBackdrop, type Palette} from '../theme';

/** Brand wordmark plus a segmented progress bar, one segment per beat. */
export const TopBar: React.FC<{beats: Beat[]; pal: Palette; brand: string}> = ({beats, pal, brand}) => {
  const frame = useCurrentFrame();
  const appear = interpolate(frame, [0, 12], [0, 1], {extrapolateRight: 'clamp'});
  const ink = onBackdrop(pal);
  return (
    <div style={{position: 'absolute', top: SAFE.top, left: SAFE.left, right: 1080 - SAFE.right + 0, opacity: appear}}>
      <div style={{display: 'flex', gap: 8, marginBottom: 22}}>
        {beats.map((b, i) => {
          const p = interpolate(frame, [b.from, b.from + b.duration], [0, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
          });
          return (
            <div key={i} style={{flex: b.duration, height: 8, borderRadius: 4, background: `${ink}2E`, overflow: 'hidden'}}>
              <div style={{width: `${p * 100}%`, height: '100%', background: pal.accent, borderRadius: 4}} />
            </div>
          );
        })}
      </div>
      <div style={{display: 'flex', alignItems: 'center', gap: 14}}>
        <div style={{width: 18, height: 18, background: pal.light ? pal.accent : pal.accent2, transform: 'rotate(45deg)', borderRadius: 3}} />
        <div style={{fontFamily: FONT, fontWeight: 800, fontSize: 34, letterSpacing: 6, color: ink, textTransform: 'uppercase'}}>{brand}</div>
      </div>
    </div>
  );
};
