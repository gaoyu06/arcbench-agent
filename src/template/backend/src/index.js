const app = require('./app');
const defaultPort = 3000;
const port = Number(process.env.PORT || defaultPort);

// Listen only after the schema and seed data exist, so no request sees a half-initialized database.
app.ready
  .then(() => {
    app.listen(port, () => {
      console.log(`Backend listening at http://127.0.0.1:${port}`);
    });
  })
  .catch((error) => {
    console.error('Database initialization failed:', error);
    process.exit(1);
  });
