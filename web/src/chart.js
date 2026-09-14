import { axisBottom, axisLeft, curveLinear, curveStepAfter, line, pointer, scaleLinear, select } from 'd3';
import { SERIES, formatValue } from './data.js';

// Charts consume display records only; estimates and metrics come from Python.
export function createChart(container, kind, duration, onInspect, tooltip) {
  const height = kind === 'roll' ? 288 : 196;
  const margin = { top: 26, right: 14, bottom: 42, left: 60 };
  const unit = kind === 'roll' ? '°' : '°/s';
  const y = scaleLinear().range([height - margin.bottom - 4, margin.top + 4]);
  const x = scaleLinear().domain([0, duration]);
  const svg = select(container).append('svg').attr('role', 'img')
    .attr('aria-label', `${kind === 'roll' ? 'Roll angle' : 'Gyroscope bias'} over time. Use the time slider for numeric readings.`);
  const series = SERIES.filter(item => item[kind] !== null);
  let scenario;
  let visible = new Set(SERIES.map(item => item.id));
  let cursorRow;
  let holding = false;
  let width = 0;

  function draw() {
    if (!scenario) return;
    width = container.getBoundingClientRect().width;
    if (width <= margin.left + margin.right + 10) return;
    x.range([margin.left + 4, width - margin.right - 4]);
    svg.attr('viewBox', `0 0 ${width} ${height}`);
    svg.selectAll('*').remove();
    const grid = svg.append('g').attr('transform', `translate(${margin.left},0)`)
      .call(axisLeft(y).ticks(5).tickSize(-(width - margin.left - margin.right)).tickFormat(''));
    grid.select('.domain').remove();
    grid.selectAll('line').attr('opacity', .45);
    const pulse = scenario.event;
    if (pulse) {
      svg.append('rect').attr('x', x(pulse.start_s)).attr('y', margin.top)
        .attr('width', x(pulse.end_s) - x(pulse.start_s))
        .attr('height', height - margin.top - margin.bottom)
        .attr('fill', 'var(--line)').attr('opacity', .3);
      if (kind === 'roll') svg.append('text').attr('x', (x(pulse.start_s) + x(pulse.end_s)) / 2)
        .attr('y', 16).attr('text-anchor', 'middle').text(
          pulse.kind === 'translation' ? 'Translation' : pulse.kind === 'bias_ramp' ? 'Bias ramp' : 'Accel. loss');
    }
    const path = line().x(row => x(row[0]));
    for (const item of series.filter(item => visible.has(item.id))) {
      path.curve(kind === 'bias' && item.id !== 'truth' ? curveStepAfter : curveLinear);
      svg.append('path').datum(scenario.rows).attr('fill', 'none')
        .attr('stroke', item.color).attr('stroke-width', item.id === 'ekf' ? 1.8 : 1.3)
        .attr('stroke-dasharray', item.dash || null)
        .attr('d', path.y(row => y(row[item[kind]])));
    }
    svg.append('g').attr('transform', `translate(0,${height - margin.bottom})`)
      .call(axisBottom(x).ticks(width < 420 ? 4 : 6).tickSizeOuter(0));
    svg.append('g').attr('transform', `translate(${margin.left},0)`)
      .call(axisLeft(y).ticks(5).tickSizeOuter(0));
    svg.append('text').attr('class', 'axis-title').attr('x', margin.left)
      .attr('y', 16).text(kind === 'roll' ? 'Roll (°)' : 'Bias (°/s)');
    svg.append('text').attr('class', 'axis-title').attr('x', (margin.left + width - margin.right) / 2)
      .attr('y', height - 4).attr('text-anchor', 'middle').text('Time (s)');
    svg.append('line').attr('class', 'cursor').attr('stroke', 'var(--accent)')
      .attr('stroke-width', 1).attr('stroke-dasharray', '2 3')
      .attr('y1', margin.top).attr('y2', height - margin.bottom);
    svg.append('g').attr('class', 'cursor-points');
    svg.append('rect').attr('fill', 'transparent').attr('x', margin.left).attr('y', margin.top)
      .attr('width', width - margin.left - margin.right)
      .attr('height', height - margin.top - margin.bottom)
      .style('touch-action', 'pan-y').style('cursor', 'crosshair')
      .on('pointerdown', function(event) {
        holding = true;
        this.setPointerCapture(event.pointerId);
        inspect(event, true);
      })
      .on('pointermove', event => {
        if (event.pointerType === 'mouse' || holding) inspect(event, holding);
      })
      .on('pointerup', () => { holding = false; tooltip.hidden = true; })
      .on('pointercancel lostpointercapture', () => { holding = false; tooltip.hidden = true; })
      .on('pointerleave', () => { if (!holding) tooltip.hidden = true; });
    if (cursorRow) setCursor(cursorRow);
  }

  function inspect(event, pause) {
    const time = Math.max(0, Math.min(duration, x.invert(pointer(event, svg.node())[0])));
    const row = onInspect(time, pause);
    if (!row) { tooltip.hidden = true; return; }
    tooltip.replaceChildren();
    const title = document.createElement('p');
    title.textContent = `${formatValue(row[0])} s · ${kind === 'bias' ? 'truth interpolated; estimates held' : 'display interpolation'}`;
    tooltip.append(title);
    for (const item of series.filter(item => visible.has(item.id))) {
      const entry = document.createElement('div');
      const label = document.createElement('span');
      label.textContent = item.label;
      const value = document.createElement('strong');
      value.textContent = `${formatValue(row[item[kind]], kind === 'roll' ? 2 : 3)}${unit}`;
      entry.append(label, value);
      tooltip.append(entry);
    }
    tooltip.hidden = false;
    const box = tooltip.getBoundingClientRect();
    tooltip.style.left = `${Math.max(8, Math.min(event.clientX + 16, window.innerWidth - box.width - 8))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(event.clientY + 16, window.innerHeight - box.height - 8))}px`;
  }

  function setCursor(row) {
    cursorRow = row;
    svg.select('.cursor').attr('x1', x(row[0])).attr('x2', x(row[0]));
    svg.select('.cursor-points').selectAll('circle')
      .data(series.filter(item => visible.has(item.id)), item => item.id).join('circle')
      .attr('cx', x(row[0])).attr('cy', item => y(row[item[kind]]))
      .attr('r', 3).attr('fill', 'var(--bg)').attr('stroke', item => item.color)
      .attr('stroke-width', 1.5);
  }

  const observer = new ResizeObserver(draw);
  observer.observe(container);
  document.fonts?.ready.then(draw);
  return {
    setData(nextScenario, nextVisible) {
      scenario = nextScenario;
      visible = nextVisible;
      const domain = scenario.domains[kind === 'roll' ? 'roll_deg' : 'bias_deg_s'];
      const spread = Math.max(domain[1] - domain[0], kind === 'roll' ? 1 : 0.1);
      y.domain([domain[0] - spread * .08, domain[1] + spread * .08]).nice();
      draw();
    },
    setCursor,
    destroy() { observer.disconnect(); svg.remove(); },
  };
}
