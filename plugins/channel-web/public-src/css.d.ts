/**
 * The side-effect stylesheet imports (this page's own sheet, and the design
 * system's) carry no module shape — declare them so the TypeScript program
 * accepts the imports the bundler extracts into the one emitted CSS asset.
 */
declare module '*.css';
