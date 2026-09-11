import {defineConfig, devices} from '@playwright/test';

export default defineConfig({
  testDir: './tests', fullyParallel: true, timeout: 30000,
  use: {baseURL:'http://127.0.0.1:5173', screenshot:'only-on-failure', trace:'retain-on-failure'},
  projects:[{name:'desktop',use:{...devices['Desktop Chrome']}},{name:'mobile',use:{...devices['iPhone 13'],defaultBrowserType:'chromium'}}],
  webServer: [
    {command: (process.platform === 'win32' ? '"..\\.venv-web\\Scripts\\python.exe"' : 'python') + ' ../tests_web/browser_server.py',url:'http://127.0.0.1:8080/api/v1/health',reuseExistingServer:!process.env.CI,timeout:30000},
    {command:'npm run dev -- --port 5173 --strictPort',url:'http://127.0.0.1:5173',reuseExistingServer:!process.env.CI,timeout:30000},
  ],
});
