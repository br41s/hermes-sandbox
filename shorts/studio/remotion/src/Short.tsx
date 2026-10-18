import React from 'react';
import {AbsoluteFill, Sequence} from 'remotion';
import type {ShortProps} from './types';
import {palette} from './theme';
import {Background} from './components/Background';
import {TopBar} from './components/TopBar';
import {Karaoke} from './components/Karaoke';
import {AvatarScene, CtaScene, HookScene, ListScene, PointScene, PresenterScene, QuoteScene, StatScene} from './components/Scenes';
import {TipShort} from './Tip';
import {Thumb} from './Cover';
import './fonts';

// The Shorts shelf shows a frame YouTube picks, not the thumbnail set through the API
// (thumbnails.set only reaches search and the channel grid). So the first half second
// IS the thumbnail: the frame YouTube is most likely to pick, and Brais can choose it
// in Studio. Under 1 s, so QA's freeze check (d=1.0) does not flag it.
export const COVER_FRAMES = 15;

export const Short: React.FC<ShortProps> = (props) => (
  <AbsoluteFill>
    {props.format === 'tip' ? <TipShort {...props} /> : <Classic {...props} />}
    <Sequence from={0} durationInFrames={COVER_FRAMES} name="portada">
      <Thumb {...props} />
    </Sequence>
  </AbsoluteFill>
);

const Classic: React.FC<ShortProps> = (props) => {
  const pal = palette(props.palette);
  const seed = `${props.palette}-${props.motif}-${props.beats.length}`;
  return (
    <AbsoluteFill style={{backgroundColor: pal.bg}}>
      <Background beats={props.beats} pal={pal} motif={props.motif} seed={seed} />
      {props.beats.map((beat, i) => {
        const scene = (() => {
          if (beat.presenter) return <PresenterScene beat={beat} pal={pal} brand={props.brand.site} />;
          switch (beat.kind) {
            case 'hook': return <HookScene beat={beat} pal={pal} />;
            case 'point': return <PointScene beat={beat} pal={pal} />;
            case 'stat': return <StatScene beat={beat} pal={pal} />;
            case 'list': return <ListScene beat={beat} pal={pal} />;
            case 'quote': return <QuoteScene beat={beat} pal={pal} />;
            case 'avatar': return <AvatarScene beat={beat} pal={pal} />;
            case 'cta': return <CtaScene beat={beat} pal={pal} brand={props.brand.site} />;
            default: return null;
          }
        })();
        return (
          <Sequence key={i} from={beat.from} durationInFrames={beat.duration} name={`${i}-${beat.kind}`}>
            {scene}
          </Sequence>
        );
      })}
      <TopBar beats={props.beats} pal={pal} brand={props.brand.name} />
      <Karaoke words={props.words} pal={pal} />
    </AbsoluteFill>
  );
};
