import React from 'react';
import {interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import type {Word} from '../types';
import {FONT, SAFE, type Palette} from '../theme';

// Word-level captions. Timings come from Edge TTS WordBoundary events (or, for
// avatar clips, are spread across the clip), so the highlight tracks the voice.
const LINGER_MS = 350;

export const Karaoke: React.FC<{words: Word[]; pal: Palette}> = ({words, pal}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const t = (frame / fps) * 1000;

  // Current page = page of the last word that has started.
  let current = -1;
  for (const w of words) {
    if (w.startMs <= t) current = w.page;
    else break;
  }
  if (current < 0) return null;
  const pageWords = words.filter((w) => w.page === current);
  const last = pageWords[pageWords.length - 1];
  const next = words.find((w) => w.page === current + 1);
  if (t > last.endMs + LINGER_MS && (!next || t < next.startMs)) return null;

  const pageStart = pageWords[0].startMs;
  const pop = interpolate(t - pageStart, [0, 120], [0.92, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const chars = pageWords.reduce((n, w) => n + w.text.length + 1, 0);
  const fontSize = chars > 22 ? 56 : chars > 16 ? 62 : 68;

  return (
    <div
      style={{
        position: 'absolute',
        left: SAFE.left,
        right: 1080 - SAFE.right,
        bottom: 1920 - SAFE.bottom + 180,
        display: 'flex',
        flexWrap: 'wrap',
        justifyContent: 'center',
        gap: '6px 14px',
        transform: `scale(${pop})`,
      }}
    >
      {pageWords.map((w, i) => {
        const active = t >= w.startMs && t < (pageWords[i + 1]?.startMs ?? w.endMs + LINGER_MS);
        const spoken = t >= w.startMs;
        return (
          <span
            key={`${w.startMs}-${i}`}
            style={{
              fontFamily: FONT,
              fontWeight: 800,
              fontSize,
              lineHeight: 1.15,
              padding: '2px 12px',
              borderRadius: 14,
              color: active ? pal.bg : pal.text,
              background: active ? pal.accent : 'transparent',
              opacity: spoken ? 1 : 0.55,
              textShadow: active ? 'none' : '0 4px 18px rgba(0,0,0,0.85), 0 0 2px rgba(0,0,0,0.9)',
              transform: active ? 'scale(1.06)' : 'none',
            }}
          >
            {w.text}
          </span>
        );
      })}
    </div>
  );
};
