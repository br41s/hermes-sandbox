import React from 'react';
import {AbsoluteFill, Sequence} from 'remotion';
import type {ShortProps} from './types';
import {palette} from './theme';
import {Background} from './components/Background';
import {TopBar} from './components/TopBar';
import {Karaoke} from './components/Karaoke';
import {AvatarScene, CtaScene, HookScene, ListScene, PointScene, QuoteScene, StatScene} from './components/Scenes';
import './fonts';

export const Short: React.FC<ShortProps> = (props) => {
  const pal = palette(props.palette);
  const seed = `${props.palette}-${props.motif}-${props.beats.length}`;
  return (
    <AbsoluteFill style={{backgroundColor: pal.bg}}>
      <Background beats={props.beats} pal={pal} motif={props.motif} seed={seed} />
      {props.beats.map((beat, i) => {
        const scene = (() => {
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
