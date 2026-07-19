const fs = require('fs');
const content = fs.readFileSync('frontend/src/hooks/useAudioSession.js', 'utf8');
if (content.includes('dummyGain')) {
  console.log("dummyGain is present in the file");
}
