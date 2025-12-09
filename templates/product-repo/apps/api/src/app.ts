import cors from 'cors';
import express, { type Express } from 'express';
import helmet from 'helmet';

import { errorHandler } from './middleware/error-handler.js';
import { apiRouter } from './routes/index.js';

export const app: Express = express();

// Security middleware
app.use(helmet());
app.use(
  cors({
    origin: process.env.CORS_ORIGIN ?? 'http://localhost:5173',
    credentials: true,
  })
);

// Body parsing
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// Health check endpoint
app.get('/health', (_req, res) => {
  res.json({
    status: 'ok',
    timestamp: new Date().toISOString(),
    version: process.env.npm_package_version ?? '0.0.0',
  });
});

// API routes
app.use('/api', apiRouter);

// Error handling (must be last)
app.use(errorHandler);
