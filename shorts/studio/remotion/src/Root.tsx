import React from 'react';
import {Composition, Still} from 'remotion';
import {Short} from './Short';
import {Cover, Thumb} from './Cover';
import {Flow} from './Flow';
import type {FlowProps, ShortProps} from './types';
import sample from './sample-props.json';

const defaults = sample as unknown as ShortProps;

const flowDefaults: FlowProps = {
  lang: 'es',
  footage: 'flow/footage.mp4',
  footageFrames: 240,
  outroFrom: 225,
  outroFrames: 150,
  durationInFrames: 375,
  words: [],
  articleUrl: 'https://biglobster.top/',
  brand: {name: 'BigLobster', site: 'biglobster.top'},
};

export const RemotionRoot: React.FC = () => (
  <>
    <Composition
      id="Short"
      component={Short}
      width={1080}
      height={1920}
      fps={30}
      durationInFrames={defaults.durationInFrames}
      defaultProps={defaults}
      calculateMetadata={({props}) => ({durationInFrames: props.durationInFrames})}
    />
    <Composition
      id="Flow"
      component={Flow}
      width={1080}
      height={1920}
      fps={30}
      durationInFrames={flowDefaults.durationInFrames}
      defaultProps={flowDefaults}
      calculateMetadata={({props}) => ({durationInFrames: props.durationInFrames})}
    />
    <Still id="Cover" component={Cover} width={1080} height={1920} defaultProps={defaults} />
    <Still id="Thumb" component={Thumb} width={1080} height={1920} defaultProps={defaults} />
  </>
);
