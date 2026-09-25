import React from 'react';
import {Composition, Still} from 'remotion';
import {Short} from './Short';
import {Cover, Thumb} from './Cover';
import type {ShortProps} from './types';
import sample from './sample-props.json';

const defaults = sample as unknown as ShortProps;

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
    <Still id="Cover" component={Cover} width={1080} height={1920} defaultProps={defaults} />
    <Still id="Thumb" component={Thumb} width={1080} height={1920} defaultProps={defaults} />
  </>
);
