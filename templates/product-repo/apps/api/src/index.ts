import { app } from './app.js';

const PORT = Number(process.env.PORT) || 3001;

app.listen(PORT, () => {
  console.info(`API server running on http://localhost:${String(PORT)}`);
  console.info(`Health check: http://localhost:${String(PORT)}/health`);
});
