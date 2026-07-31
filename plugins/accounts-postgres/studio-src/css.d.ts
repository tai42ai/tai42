/**
 * The side-effect stylesheet import (the plugin's own scoped stylesheet) carries
 * no module shape — declare it so the TypeScript program accepts the import the
 * bundler extracts into the plugin's one emitted CSS asset.
 */
declare module '*.css';
