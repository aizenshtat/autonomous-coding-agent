import { Router, type Router as RouterType } from 'express';

export const apiRouter: RouterType = Router();

// Example route - customize for your app
apiRouter.get('/', (_req, res) => {
  res.json({ message: 'API is running' });
});

// Add your routes here
// Example:
// apiRouter.use('/users', usersRouter);
// apiRouter.use('/auth', authRouter);
