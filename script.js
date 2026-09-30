(() => {
  document.addEventListener('DOMContentLoaded', () => {
    // Telemetry: send page view gracefully
    try {
      fetch('https://mqtt.example.com/portfolio/visit', {
        method: 'POST',
        keepalive: true,
        signal: typeof AbortSignal !== 'undefined' && AbortSignal.timeout ? AbortSignal.timeout(5000) : undefined
      }).catch(() => {});
    } catch (e) {}

    // Hash navigation scroll handling with selector error prevention
    if (window.location.hash) {
      try {
        const el = document.querySelector(window.location.hash);
        if (el) setTimeout(() => el.scrollIntoView({ behavior: 'smooth' }), 300);
      } catch (e) {
        // Ignore invalid CSS selectors in location hash
      }
    }

    initCometsCanvas();
    initNavbar();
    initMobileMenu();
    initScrollReveal();
    initTelegramCopy();
    initContactsInteractions();
    loadProjectsAutomatically();
  });

  // Canvas Comets / Shooting Stars Animation — справа сверху вниз слева (Top-Right -> Down-Left), 1 комета в 6 секунд
  function initCometsCanvas() {
    const canvas = document.getElementById('cometsCanvas');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    let width, height;

    function resize() {
      width = canvas.width = window.innerWidth;
      height = canvas.height = window.innerHeight;
    }

    window.addEventListener('resize', resize);
    resize();

    class Comet {
      constructor() {
        this.active = false;
      }

      spawn() {
        this.active = true;
        // Angle: 145°..162° (dx < 0, dy > 0 -> Top-Right -> Down-Left trajectory)
        this.angle = (145 + Math.random() * 17) * (Math.PI / 180);
        this.length = Math.random() * 110 + 65;
        this.speed = Math.random() * 2.4 + 1.4;
        this.opacity = Math.random() * 0.45 + 0.2;
        this.width = Math.random() * 1.4 + 0.7;
        this.isCyan = Math.random() > 0.45;

        const margin = 60;
        if (Math.random() > 0.4) {
          // Right edge spawn (Top 0 to 85% height)
          this.x = width + margin;
          this.y = Math.random() * (height * 0.85);
        } else {
          // Top edge spawn (Right 35% to 100% width)
          this.x = width * 0.35 + Math.random() * (width * 0.65) + margin;
          this.y = -margin;
        }

        this.dx = Math.cos(this.angle); // < 0 (left)
        this.dy = Math.sin(this.angle); // > 0 (down)

        this.gradient = ctx.createLinearGradient(0, 0, -this.dx * this.length, -this.dy * this.length);
        if (this.isCyan) {
          this.gradient.addColorStop(0, `rgba(0, 229, 255, ${this.opacity})`);
          this.gradient.addColorStop(1, 'rgba(0, 229, 255, 0)');
        } else {
          this.gradient.addColorStop(0, `rgba(155, 109, 255, ${this.opacity})`);
          this.gradient.addColorStop(1, 'rgba(155, 109, 255, 0)');
        }
      }

      update() {
        if (!this.active) return;

        this.x += this.dx * this.speed;
        this.y += this.dy * this.speed;

        const margin = this.length + 120;
        if (
          this.x < -margin ||
          this.y > height + margin
        ) {
          this.active = false;
        }
      }

      draw() {
        if (!this.active || !this.gradient) return;

        ctx.save();
        ctx.translate(this.x, this.y);
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.lineTo(-this.dx * this.length, -this.dy * this.length);
        ctx.strokeStyle = this.gradient;
        ctx.lineWidth = this.width;
        ctx.stroke();
        ctx.restore();
      }
    }

    const comets = [new Comet(), new Comet(), new Comet()];
    let isAnimating = false;

    function startAnimation() {
      if (!isAnimating) {
        isAnimating = true;
        requestAnimationFrame(animate);
      }
    }

    function trySpawn() {
      if (document.hidden) return;
      const inactive = comets.filter(c => !c.active);
      if (inactive.length > 0) {
        inactive[0].spawn();
        startAnimation();
      }
    }

    // Strictly 1 comet every 6000ms
    setTimeout(() => {
      trySpawn();
      setInterval(trySpawn, 6000);
    }, 800);

    function animate() {
      if (document.hidden) {
        isAnimating = false;
        return;
      }

      ctx.clearRect(0, 0, width, height);
      let anyActive = false;

      comets.forEach(c => {
        if (c.active) {
          c.update();
          c.draw();
          if (c.active) anyActive = true;
        }
      });

      if (anyActive) {
        requestAnimationFrame(animate);
      } else {
        ctx.clearRect(0, 0, width, height);
        isAnimating = false;
      }
    }

    // Single visibilitychange listener to prevent listener leaks
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && comets.some(c => c.active)) {
        startAnimation();
      }
    });
  }

  // Navbar scroll glass effect
  function initNavbar() {
    const navbar = document.getElementById('navbar');
    if (!navbar) return;

    window.addEventListener('scroll', () => {
      if (window.scrollY > 40) {
        navbar.classList.add('scrolled');
      } else {
        navbar.classList.remove('scrolled');
      }
    }, { passive: true });
  }

  // Mobile Menu with full accessibility and state management
  function initMobileMenu() {
    const toggle = document.getElementById('menuToggle');
    const menu = document.getElementById('mobileMenu');
    if (!toggle || !menu) return;

    function openMenu() {
      menu.classList.add('active');
      toggle.classList.add('active');
      toggle.setAttribute('aria-expanded', 'true');
      menu.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
    }

    function closeMenu() {
      menu.classList.remove('active');
      toggle.classList.remove('active');
      toggle.setAttribute('aria-expanded', 'false');
      menu.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
    }

    toggle.addEventListener('click', () => {
      const isActive = menu.classList.contains('active');
      if (isActive) closeMenu(); else openMenu();
    });

    menu.querySelectorAll('a').forEach(link => {
      link.addEventListener('click', () => {
        closeMenu();
      });
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && menu.classList.contains('active')) {
        closeMenu();
      }
    });
  }

  // Singleton IntersectionObserver for Scroll Reveal Animations
  let scrollObserver = null;

  function initScrollReveal() {
    if (!scrollObserver) {
      scrollObserver = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
          if (entry.isIntersecting) {
            entry.target.classList.add('visible');
            scrollObserver.unobserve(entry.target);
          }
        });
      }, {
        threshold: 0.1,
        rootMargin: '0px 0px -30px 0px'
      });
    }

    const reveals = document.querySelectorAll('.reveal:not(.visible)');
    reveals.forEach(el => scrollObserver.observe(el));
  }

  // Toast & Telegram Copy
  function initTelegramCopy() {
    const copyBtn = document.getElementById('copyTelegramBtn');
    const copyText = document.getElementById('copyBtnText');

    copyBtn?.addEventListener('click', () => {
      navigator.clipboard.writeText('@w1bbex').then(() => {
        if (copyText) copyText.textContent = 'Скопировано ✓';
        showToast('Telegram @w1bbex скопирован в буфер!');
        setTimeout(() => {
          if (copyText) copyText.textContent = '@w1bbex';
        }, 2200);
      }).catch(err => {
        console.error('Не удалось скопировать текст: ', err);
        showToast('Не удалось скопировать. Попробуйте вручную.');
      });
    });
  }

  // Contacts block — ripple on pills + 3D tilt
  function initContactsInteractions() {
    // Ripple on every .contact-pill
    document.querySelectorAll('.contact-pill').forEach(pill => {
      pill.addEventListener('click', function(e) {
        const rect = this.getBoundingClientRect();
        const size = Math.max(rect.width, rect.height);
        const x = e.clientX - rect.left - size / 2;
        const y = e.clientY - rect.top  - size / 2;
        const ripple = document.createElement('span');
        ripple.className = 'pill-ripple';
        ripple.style.cssText = `width:${size}px;height:${size}px;left:${x}px;top:${y}px;`;
        this.appendChild(ripple);
        ripple.addEventListener('animationend', () => ripple.remove());
      });
    });

    // 3D tilt on contacts card
    makeTiltCard(document.querySelector('.contacts-card-block'), {
      maxTiltX: 4, maxTiltY: 6, lerpSpeed: 0.1
    });

    // 3D tilt on hero code box
    makeTiltCard(document.querySelector('.hero-code-box'), {
      maxTiltX: 5, maxTiltY: 8, lerpSpeed: 0.11, glowColor: '0,229,255'
    });
  }

  /**
   * Generic smooth 3D tilt card.
   * Uses rAF lerp loop — no transition conflicts, no edge bugs.
   * @param {HTMLElement} el
   * @param {{ maxTiltX, maxTiltY, lerpSpeed, glowColor }} opts
   */
  function makeTiltCard(el, {
    maxTiltX   = 4,
    maxTiltY   = 6,
    lerpSpeed  = 0.1,
    glowColor  = '107,63,255'
  } = {}) {
    if (!el) return;

    let tX = 0, tY = 0;   // target
    let cX = 0, cY = 0;   // current (lerped)
    let hovering = false;
    let rafId = null;
    let currentLerp = lerpSpeed;

    function lerp(a, b, t) { return a + (b - a) * t; }

    function tick() {
      if (hovering) {
        currentLerp = Math.min(currentLerp + 0.005, lerpSpeed);
      }

      cX = lerp(cX, tX, currentLerp);
      cY = lerp(cY, tY, currentLerp);

      el.style.transform = `perspective(900px) rotateX(${cX}deg) rotateY(${cY}deg) translateZ(8px)`;

      const intensity = Math.sqrt(cX * cX + cY * cY) / Math.sqrt(maxTiltX * maxTiltX + maxTiltY * maxTiltY);
      el.style.borderColor = `rgba(155, 109, 255, ${0.1 + intensity * 0.55})`;
      el.style.boxShadow   = `0 24px 70px rgba(0,0,0,0.6),
        0 0 ${18 + intensity * 50}px rgba(${glowColor}, ${0.07 + intensity * 0.22}),
        inset 0 1px 0 rgba(255,255,255,${0.03 + intensity * 0.06})`;

      if (hovering || Math.abs(cX - tX) > 0.005 || Math.abs(cY - tY) > 0.005) {
        rafId = requestAnimationFrame(tick);
      } else {
        el.style.transform   = '';
        el.style.borderColor = '';
        el.style.boxShadow   = '';
        rafId = null;
      }
    }

    function startLoop() {
      if (!rafId) rafId = requestAnimationFrame(tick);
    }

    const wrapper = el.closest('.tilt-wrapper') || el.parentElement;
    let wrapperRect = null;

    function updateRect() {
      if (wrapper) wrapperRect = wrapper.getBoundingClientRect();
    }

    wrapper.addEventListener('mouseenter', () => {
      hovering = true;
      currentLerp = 0.02;
      updateRect();
    });

    wrapper.addEventListener('mousemove', (e) => {
      if (!wrapperRect) updateRect();
      const dx = (e.clientX - (wrapperRect.left + wrapperRect.width  / 2)) / (wrapperRect.width  / 2);
      const dy = (e.clientY - (wrapperRect.top  + wrapperRect.height / 2)) / (wrapperRect.height / 2);
      
      tX = -dy * maxTiltX;
      tY =  dx * maxTiltY;
      startLoop();
    });

    wrapper.addEventListener('mouseleave', () => {
      hovering = false;
      currentLerp = 0.04;
      tX = 0;
      tY = 0;
      wrapperRect = null;
      startLoop();
    });
  }

  function showToast(message) {
    const toast = document.getElementById('toastNotification');
    const toastText = document.getElementById('toastText');

    if (!toast || !toastText) return;

    toastText.textContent = message;
    toast.classList.add('show');

    setTimeout(() => {
      toast.classList.remove('show');
    }, 3000);
  }

  // Fallback Embedded Data (if API / json fails)
  const fallbackProjects = [
    {
      "id": "error-fallback",
      "featured": true,
      "badge": "Ошибка",
      "title": "Проекты временно не загрузились",
      "subtitle": "Отсутствует связь с источником данных",
      "category": "Системное сообщение",
      "problem": "Не удалось получить список проектов из API.",
      "solution": "Перезагрузите страницу или откройте портфолио немного позже.",
      "scale": "-",
      "tags": ["Error"]
    }
  ];

  async function loadProjectsAutomatically() {
    const container = document.getElementById('projectsGrid');
    if (!container) return;
    // If projects are already hardcoded in HTML, preserve them for instant 100% reliable rendering
    if (container.children.length > 0) return;

    let projects = [];
    try {
      const res = await fetch('projects.json');
      if (res.ok) {
        projects = await res.json();
      } else {
        projects = fallbackProjects;
      }
    } catch (err) {
      projects = fallbackProjects;
    }

    renderProjectsList(projects, container);
  }

  function renderProjectsList(projects, container) {
    container.innerHTML = '';

    projects.forEach((proj) => {
      const card = document.createElement('div');
      card.className = `glass-card project-card reveal ${proj.featured ? 'featured-card' : ''}`;

      card.innerHTML = `
        <div class="project-header-top">
          ${proj.badge ? `<span class="project-badge">${proj.badge}</span>` : ''}
          <span class="project-category">${proj.category}</span>
          <h3 class="project-title">${proj.title}</h3>
          <p class="project-subtitle">${proj.subtitle}</p>
        </div>

        <div class="project-details">
          <div class="detail-block">
            <span class="detail-label">Проблема</span>
            <p>${proj.problem}</p>
          </div>

          <div class="detail-block">
            <span class="detail-label">Решение &amp; Процесс</span>
            <p>${proj.solution}</p>
          </div>

          <div class="detail-block">
            <span class="detail-label">Масштаб / Метрики</span>
            <p>${proj.scale}</p>
          </div>

          ${proj.featured && proj.architecture ? `
          <div class="detail-block architecture-block">
            <span class="detail-label">Архитектура</span>
            <code class="arch-code">${proj.architecture}</code>
          </div>
          ` : ''}

          ${proj.featured && proj.lesson ? `
          <div class="detail-block lesson-block">
            <span class="detail-label">Что пошло не так / Урок</span>
            <p>${proj.lesson}</p>
          </div>
          ` : ''}
        </div>

        <div class="project-footer-tags">
          <div class="skill-tags">
            ${proj.tags.map(t => `<span class="tag">${t}</span>`).join('')}
          </div>
          ${proj.demoUrl ? `
          <a href="${proj.demoUrl}" target="_blank" rel="noopener noreferrer" class="project-demo-link">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
            Открыть демо
          </a>` : ''}
        </div>
      `;

      container.appendChild(card);
    });

    initScrollReveal();
  }
})();
