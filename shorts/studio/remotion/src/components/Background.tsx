import React from 'react';
import {AbsoluteFill, OffthreadVideo, Sequence, interpolate, random, staticFile, useCurrentFrame} from 'remotion';
import type {Beat} from '../types';
import type {Palette} from '../theme';

const W = 1080;
const H = 1920;

/** One motif shape, drawn centred on (0,0) at unit size. */
const Shape: React.FC<{motif: string; size: number; color: string; filled: boolean}> = ({motif, size, color, filled}) => {
  const s = size;
  const stroke = filled ? undefined : color;
  const fill = filled ? color : 'none';
  const sw = Math.max(3, s * 0.06);
  switch (motif) {
    case 'diamonds':
      return <rect x={-s / 2} y={-s / 2} width={s} height={s} transform="rotate(45)" fill={fill} stroke={stroke} strokeWidth={sw} rx={s * 0.08} />;
    case 'circles':
      return <circle r={s / 2} fill={fill} stroke={stroke} strokeWidth={sw} />;
    case 'rings':
      return <circle r={s / 2} fill="none" stroke={color} strokeWidth={sw * 1.6} />;
    case 'chevrons':
      return <polyline points={`${-s / 2},${-s / 3} 0,${s / 3} ${s / 2},${-s / 3}`} fill="none" stroke={color} strokeWidth={sw * 1.5} strokeLinecap="round" strokeLinejoin="round" />;
    case 'dots':
      return <circle r={s / 6} fill={color} />;
    case 'bars':
      return <rect x={-s / 8} y={-s / 2} width={s / 4} height={s} rx={s / 8} fill={color} />;
    case 'grid':
      return <rect x={-s / 2} y={-s / 2} width={s} height={s} fill="none" stroke={color} strokeWidth={sw * 0.6} />;
    case 'waves':
      return <path d={`M ${-s / 2} 0 Q ${-s / 4} ${-s / 4} 0 0 T ${s / 2} 0`} fill="none" stroke={color} strokeWidth={sw * 1.3} strokeLinecap="round" />;
    default:
      return <circle r={s / 2} fill={fill} />;
  }
};

/** Slowly drifting motif field. Seeded, so a re-render is identical. */
export const MotifField: React.FC<{motif: string; pal: Palette; seed: string; intensity?: number}> = ({motif, pal, seed, intensity = 1}) => {
  const frame = useCurrentFrame();
  const count = motif === 'dots' ? 26 : 14;
  const shapes = Array.from({length: count}, (_, i) => {
    const r = (k: string) => random(`${seed}-${motif}-${i}-${k}`);
    return {
      x: r('x') * W,
      y: r('y') * H,
      size: 60 + r('s') * 170,
      speed: 0.15 + r('v') * 0.35,
      spin: (r('r') - 0.5) * 0.4,
      phase: r('p') * Math.PI * 2,
      color: r('c') > 0.5 ? pal.accent : pal.accent2,
      filled: r('f') > 0.6,
      opacity: (0.07 + r('o') * 0.12) * intensity,
    };
  });
  return (
    <svg width={W} height={H} style={{position: 'absolute', inset: 0}}>
      {shapes.map((sh, i) => {
        const y = (sh.y - frame * sh.speed + H * 2) % (H + 300) - 150;
        const x = sh.x + Math.sin(frame / 60 + sh.phase) * 24;
        const rot = frame * sh.spin + sh.phase * 30;
        return (
          <g key={i} transform={`translate(${x} ${y}) rotate(${rot})`} opacity={sh.opacity}>
            <Shape motif={motif} size={sh.size} color={sh.color} filled={sh.filled} />
          </g>
        );
      })}
    </svg>
  );
};

/** Gradient base, motif, and the current beat's B-roll under a dark scrim. */
export const Background: React.FC<{beats: Beat[]; pal: Palette; motif: string; seed: string}> = ({beats, pal, motif, seed}) => {
  const frame = useCurrentFrame();
  const drift = interpolate(frame, [0, 1800], [0, 30], {extrapolateRight: 'clamp'});
  return (
    <AbsoluteFill style={{background: `linear-gradient(${160 + drift}deg, ${pal.bg} 0%, ${pal.bg2} 55%, ${pal.bg} 100%)`}}>
      {beats.map((b, i) =>
        b.broll && b.kind !== 'avatar' ? (
          <Sequence key={i} from={b.from} durationInFrames={b.duration} layout="none">
            <BrollClip src={b.broll} duration={b.duration} />
          </Sequence>
        ) : null,
      )}
      {/* Scrim: keeps text legible over any footage, darker at top and bottom where UI sits. */}
      <AbsoluteFill
        style={{
          background: `linear-gradient(180deg, ${pal.bg}E6 0%, ${pal.bg}8C 30%, ${pal.bg}99 60%, ${pal.bg}F0 100%)`,
        }}
      />
      <MotifField motif={motif} pal={pal} seed={seed} />
    </AbsoluteFill>
  );
};

const BrollClip: React.FC<{src: string; duration: number}> = ({src, duration}) => {
  const frame = useCurrentFrame();
  // Short cross-fade in and out, plus a slow push-in, so a cut never lands on a still frame.
  const opacity = interpolate(frame, [0, 8, duration - 8, duration], [0, 0.42, 0.42, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const scale = interpolate(frame, [0, duration], [1.04, 1.12]);
  return (
    <AbsoluteFill style={{opacity}}>
      <OffthreadVideo src={staticFile(src)} muted style={{width: '100%', height: '100%', objectFit: 'cover', transform: `scale(${scale})`}} />
    </AbsoluteFill>
  );
};
