import { useState } from 'react';

const VOICES = [
  { id: 'en-US-JennyNeural',   label: 'Jenny',   accent: 'US', gender: 'Female' },
  { id: 'en-US-GuyNeural',     label: 'Guy',     accent: 'US', gender: 'Male' },
  { id: 'en-US-AriaNeural',    label: 'Aria',    accent: 'US', gender: 'Female' },
  { id: 'en-GB-SoniaNeural',   label: 'Sonia',   accent: 'UK', gender: 'Female' },
  { id: 'en-GB-RyanNeural',    label: 'Ryan',    accent: 'UK', gender: 'Male' },
  { id: 'en-IN-NeerjaNeural',  label: 'Neerja',  accent: 'IN', gender: 'Female' },
  { id: 'en-IN-PrabhatNeural', label: 'Prabhat', accent: 'IN', gender: 'Male' },
];

const MODELS = [
  { id: 'base.en',          label: 'Base (EN)',         size: '74M',   speed: 'Very Fast' },
  { id: 'small.en',         label: 'Small (EN)',        size: '244M',  speed: 'Fast' },
  { id: 'medium.en',        label: 'Medium (EN)',       size: '769M',  speed: 'Moderate' },
  { id: 'distil-large-v3',  label: 'Distil Large v3',  size: '756M',  speed: 'Fast' },
  { id: 'large-v3',         label: 'Large v3',          size: '1.5B',  speed: 'Slow' },
];

export default function SetupScreen({ onStart }) {
  const [voice, setVoice] = useState('en-IN-PrabhatNeural');
  const [model, setModel] = useState('base.en');

  const handleStart = () => {
    onStart(voice, model);
  };

  return (
    <div className="setup-container">
      <div className="bg-mesh" />
      <div className="setup-card">
        <div className="setup-header">
          <div className="setup-logo">🎓</div>
          <h1>Study Coach</h1>
          <p>Your AI-powered voice study companion.<br />Select your preferences and start a session.</p>
        </div>

        <div className="form-group">
          <label htmlFor="voice-select">Coach Voice</label>
          <select
            id="voice-select"
            value={voice}
            onChange={(e) => setVoice(e.target.value)}
          >
            {VOICES.map(v => (
              <option key={v.id} value={v.id}>
                {v.label} ({v.accent} {v.gender})
              </option>
            ))}
          </select>
        </div>

        <div className="form-group">
          <label htmlFor="model-select">Speech Recognition Model</label>
          <select
            id="model-select"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          >
            {MODELS.map(m => (
              <option key={m.id} value={m.id}>
                {m.label} — {m.size} — {m.speed}
              </option>
            ))}
          </select>
        </div>

        <button className="start-btn" onClick={handleStart}>
          Start Session →
        </button>
      </div>
    </div>
  );
}
