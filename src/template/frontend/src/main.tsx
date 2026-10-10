import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import './index.css';

// Uncaught errors in the page are sent to the backend log, where the check tool reports them.
const reportError = (message: string) =>
  fetch('/api/client-errors', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: message.slice(0, 500), page: location.pathname }),
    keepalive: true,
  }).catch(() => {});
window.addEventListener('error', (event) => reportError(String(event.error?.stack || event.message)));
window.addEventListener('unhandledrejection', (event) => reportError(String(event.reason?.stack || event.reason)));

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    {/* Commit route changes synchronously, so the next page is on screen as soon as a navigation is clicked. */}
    <BrowserRouter unstable_useTransitions={false}>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
