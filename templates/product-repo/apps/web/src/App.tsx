import { Button } from '@/components/ui/button';
import { ThemeToggle } from '@/components/theme-toggle';

function App() {
  return (
    <div className="min-h-screen bg-background text-foreground p-4 md:p-8">
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-2xl font-bold">Hello World</h1>
        <ThemeToggle />
      </div>
      <div className="space-y-4">
        <Button>Primary Button</Button>
        <Button variant="secondary">Secondary Button</Button>
        <Button variant="destructive">Destructive Button</Button>
        <Button variant="outline">Outline Button</Button>
      </div>
    </div>
  );
}

export default App