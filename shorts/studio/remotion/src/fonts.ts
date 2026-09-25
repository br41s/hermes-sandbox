import {loadFont} from '@remotion/fonts';
import {staticFile} from 'remotion';
import {FONT} from './theme';

// Inter (variable, OFL). build.py copies the woff2 from @fontsource-variable/inter
// into the job's public dir, so a render never reaches Google Fonts.
export const fontReady = loadFont({
  family: FONT,
  url: staticFile('fonts/inter-latin-wght-normal.woff2'),
  weight: '100 900',
});
