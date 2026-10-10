const express = require('express');
const cors = require('cors');
const bodyParser = require('body-parser');
const fs = require('fs');
const path = require('path');
const app = express();

// route modules imports

// middleware imports
app.use(cors());
app.use(bodyParser.json());

// API requests are handled one at a time in arrival order, so a write sent just before a page reload is
// stored before the reload's reads run. A request whose connection already closed is skipped (res.closed;
// not req.destroyed, which newer Node versions set as soon as body-parser has read the body).
// A request still running after API_SLOT_MS lets later requests through, so one handler that never
// answers cannot stall every other request.
const API_SLOT_MS = 10000;
let apiQueue = Promise.resolve();
app.use('/api', (req, res, next) => {
  apiQueue = apiQueue.then(() => new Promise((resolve) => {
    if (res.closed) {
      resolve();
      return;
    }
    const timer = setTimeout(() => {
      console.error(`[api] ${req.method} ${req.originalUrl} has not answered after ${API_SLOT_MS} ms`);
      resolve();
    }, API_SLOT_MS);
    const release = () => {
      clearTimeout(timer);
      resolve();
    };
    res.on('finish', release);
    res.on('close', release);
    next();
  }));
});

// Startup work. index.js starts listening only after app.ready resolves, so chain every startup step
// (schema, seed data) onto this promise instead of starting it in the background.
const { initializeDatabase } = require('./database/init_db');
app.ready = initializeDatabase();

// register routes
app.get('/api/health', (req, res) => {
  res.json({ code: 200, message: 'Backend Ready' });
});

// Uncaught errors from the page (see frontend/src/main.tsx), logged for the check tool.
app.post('/api/client-errors', (req, res) => {
  const { message = '', page = '' } = req.body || {};
  console.error(`[browser error] ${String(page).slice(0, 200)} ${String(message).slice(0, 500).replace(/\s+/g, ' ')}`);
  res.status(204).end();
});

const frontendDistPath = path.resolve(__dirname, '../../frontend/dist');

if (fs.existsSync(frontendDistPath)) {
  app.use(express.static(frontendDistPath));

  // Keep API routes on the backend and serve the SPA for all other GET requests.
  app.get(/^(?!\/api(?:\/|$)).*/, (req, res) => {
    res.sendFile(path.join(frontendDistPath, 'index.html'));
  });
} else {
  app.get('/', (req, res) => {
    res
      .status(503)
      .type('html')
      .send(`<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Frontend Build Missing</title>
    <style>
      body {
        margin: 0;
        font-family: ui-sans-serif, system-ui, sans-serif;
        background: #f6f7f9;
        color: #1f2937;
      }
      main {
        max-width: 720px;
        margin: 12vh auto 0;
        padding: 24px;
      }
      section {
        background: #fff;
        border: 1px solid #d1d5db;
        border-radius: 12px;
        padding: 24px;
        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
      }
      h1 {
        margin-top: 0;
      }
      code {
        background: #f3f4f6;
        padding: 2px 6px;
        border-radius: 6px;
      }
    </style>
  </head>
  <body>
    <main>
      <section>
        <h1>Frontend build missing</h1>
        <p>The backend is running, but <code>frontend/dist</code> is not available yet.</p>
        <p>Build the frontend first, then start the backend so it can host the compiled site on the same port.</p>
      </section>
    </main>
  </body>
</html>`);
  });
}

module.exports = app;
