import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// The build output is committed under the Python package so `openhouse inspect`
// serves it with zero Node at runtime (see package.json). `base: './'` makes
// asset URLs relative, so index.html works when the stdlib server hands it out
// from `/`. The dev proxy is a contributor convenience: run `openhouse inspect`
// in another terminal and point INSPECT_API at the port it prints.
export default defineConfig({
  plugins: [svelte()],
  base: './',
  build: {
    outDir: '../../openhouse/inspect/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': process.env.INSPECT_API || 'http://127.0.0.1:8000',
    },
  },
})
