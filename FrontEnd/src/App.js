import React, { useState, useRef, useEffect } from 'react';
import './App.css';

function App() {
  const [prUrl, setPrUrl] = useState('');
  const [logs, setLogs] = useState('Awaiting deployment...');
  const [isDeploying, setIsDeploying] = useState(false);
  const logsEndRef = useRef(null);

  const scrollToBottom = () => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(scrollToBottom, [logs]);

  const handleDeploy = async (event) => {
    event.preventDefault();
    setLogs('Starting deployment...\n');
    setIsDeploying(true);

    try {
      const response = await fetch('/api/deploy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pr_url: prUrl }),
      });

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let accumulatedLogs = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        
        const chunk = decoder.decode(value, { stream: true });
        accumulatedLogs += chunk;
        setLogs(accumulatedLogs);
      }

    } catch (error) {
      setLogs(prev => prev + `\n--- \nClient-side error: ${error.message}`);
    } finally {
      setIsDeploying(false);
    }
  };

  return (
    <div className="container">
      <h1>Deploy GitHub PR to Docker</h1>
      <form onSubmit={handleDeploy}>
        <input
          type="url"
          value={prUrl}
          onChange={(e) => setPrUrl(e.target.value)}
          placeholder="https://github.com/owner/repo/pull/123"
          required
          disabled={isDeploying}
        />
        <button type="submit" disabled={isDeploying}>
          {isDeploying ? 'Deploying...' : 'Deploy'}
        </button>
      </form>
      <h2>Deployment Logs</h2>
      <pre className="logs-container">
        {logs}
        <div ref={logsEndRef} />
      </pre>
    </div>
  );
}

export default App;
