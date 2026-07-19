import './index.css';
import SetupScreen from './components/SetupScreen';
import SessionScreen from './components/SessionScreen';
import useAudioSession from './hooks/useAudioSession';

function App() {
  const {
    status,
    pipeline,
    messages,
    currentAiText,
    analyserRef,
    startSession,
    endSession,
  } = useAudioSession();

  const isInSession = status === 'connecting' || status === 'connected';

  const handleStart = (voice, model) => {
    startSession(voice, model);
  };

  const handleEnd = () => {
    endSession();
  };

  if (!isInSession) {
    return <SetupScreen onStart={handleStart} />;
  }

  return (
    <SessionScreen
      status={status}
      pipeline={pipeline}
      messages={messages}
      currentAiText={currentAiText}
      analyserRef={analyserRef}
      onEnd={handleEnd}
    />
  );
}

export default App;
