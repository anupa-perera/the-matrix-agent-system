from __future__ import annotations


def matrix_background_canvas() -> str:
    return '<canvas id="matrix-rain" aria-hidden="true"></canvas>'


def matrix_rain_canvas_styles(opacity: str = "0.42") -> str:
    return f"""
    #matrix-rain {{
      position: fixed;
      inset: 0;
      width: 100vw;
      height: 100vh;
      z-index: 0;
      opacity: {opacity};
      pointer-events: none;
    }}
"""


def matrix_screen_overlay_styles() -> str:
    return """
    body::before {
      content: '';
      position: fixed;
      inset: 0;
      background: radial-gradient(ellipse 95% 75% at 50% 45%, transparent 0%, rgba(0,0,0,0.25) 62%, rgba(0,0,0,0.72) 100%);
      pointer-events: none;
      z-index: 1;
    }
    body::after {
      content: '';
      position: fixed;
      inset: 0;
      background: repeating-linear-gradient(0deg, transparent 0px, transparent 2px, rgba(0,0,0,0.22) 3px, transparent 4px);
      pointer-events: none;
      z-index: 999;
      mix-blend-mode: multiply;
    }
"""


def matrix_background_styles(opacity: str = "0.42") -> str:
    return matrix_rain_canvas_styles(opacity) + matrix_screen_overlay_styles()


def matrix_rain_script(interval_ms: int = 60) -> str:
    return f"""
  <script>
    (function () {{
      const canvas = document.getElementById('matrix-rain');
      if (!canvas || !canvas.getContext) return;
      const ctx = canvas.getContext('2d');
      const glyphs = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ<>/\\\\|=+-*';
      const fontSize = 16;
      let cols;
      let drops;

      function setup() {{
        canvas.width = window.innerWidth;
        canvas.height = window.innerHeight;
        cols = Math.floor(canvas.width / fontSize);
        drops = Array.from({{ length: cols }}, () => Math.random() * -80);
      }}

      setup();
      window.addEventListener('resize', setup);

      function draw() {{
        ctx.fillStyle = 'rgba(0, 0, 0, 0.045)';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.font = fontSize + 'px "Share Tech Mono", "Cascadia Mono", monospace';
        for (let i = 0; i < cols; i++) {{
          const ch = glyphs.charAt(Math.floor(Math.random() * glyphs.length));
          const y = drops[i] * fontSize;
          ctx.fillStyle = Math.random() > 0.975 ? '#d4ffe2' : '#00b341';
          ctx.fillText(ch, i * fontSize, y);
          if (y > canvas.height && Math.random() > 0.972) drops[i] = 0;
          drops[i]++;
        }}
      }}

      setInterval(draw, {interval_ms});
    }})();
  </script>
"""
