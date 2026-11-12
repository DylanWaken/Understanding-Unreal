'use strict';
const fs = require('fs');
const path = require('path');

function findAssets() {
  const candidates = [];
  if (process.env.FRAME_ASSETS_DIR) candidates.push(path.resolve(process.env.FRAME_ASSETS_DIR));
  candidates.push(path.resolve(__dirname, '../site/assets'));
  for (let dir = __dirname; ; dir = path.dirname(dir)) {
    candidates.push(path.join(dir, 'Docs', 'Unreal', 'assets'));
    if (path.dirname(dir) === dir) break;
  }
  const result = candidates.find(dir => fs.existsSync(path.join(dir, 'frame-codec.js')) && fs.existsSync(path.join(dir, 'frame-explorer.js')));
  if (!result) throw new Error('Published explorer assets not found; set FRAME_ASSETS_DIR.');
  return result;
}

const assets = findAssets();
module.exports = {
  assets,
  package: process.env.FRAME_PACKAGE_DIR ? path.resolve(process.env.FRAME_PACKAGE_DIR) : path.join(assets, 'lumen-frame')
};
