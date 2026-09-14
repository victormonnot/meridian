import { area, axisBottom, axisLeft, curveStepAfter, line, pointer, scaleLinear, select } from 'd3';
import { diagnosticStateAt, lastInnovation, stateDiagnostic } from './diagnostics.js';

// Endpoints are held; innovation dots exist only at recorded corrections.
export function createDiagnosticChart(container, kind, duration, onInspect) {
  const height = 270;
  const margin = { top: 28, right: 14, bottom: 40, left: 66 };
  const x = scaleLinear().domain([0, duration]);
  const y = scaleLinear().range([height - margin.bottom, margin.top]);
  const svg = select(container).append('svg').attr('role', 'img');
  let scenario;
  let method;
  let component;
  let mode;
  let currentTime = 0;
  let holding = false;
  let points = [];

  function draw() {
    if (!scenario) return;
    const width = container.getBoundingClientRect().width;
    if (width < margin.left + margin.right + 10) return;
    x.range([margin.left, width - margin.right]);
    svg.attr('viewBox', `0 0 ${width} ${height}`);
    svg.selectAll('*').remove();
    let values = [0];
    let label;
    if (kind === 'state') {
      const samples = scenario.diagnostics.states.map(row => stateDiagnostic(row, method.id, component)).filter(Boolean);
      points = samples;
      label = `${component === 'roll' ? 'Roll error (°)' : 'Bias error (°/s)'}`;
      for (const sample of samples) values.push(sample.error, -2 * (sample.sigma ?? 0), 2 * (sample.sigma ?? 0));
    } else {
      const record = scenario.diagnostics[method.id];
      points = [];
      if (record) for (const row of record.rows) {
        if (mode === 'nis') points.push({ time: row[0], value: row.at(-1), component: 0 });
        else {
          points.push({ time: row[0], value: row[1], component: 0 });
          if (record.dimension === 2) points.push({ time: row[0], value: row[2], component: 1 });
        }
      }
      label = mode === 'nis' ? `NIS · ${record?.dimension ?? '—'} measurement ${record?.dimension === 1 ? 'dimension' : 'dimensions'}`
        : method.id === 'kalman' ? 'Angle innovation (°)' : 'Force innovation (m/s²)';
      values.push(...points.map(point => point.value));
      if (mode === 'nis' && record) values.push(record.dimension);
    }
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const spread = Math.max(hi - lo, .1);
    y.domain([lo - .08 * spread, hi + .08 * spread]).nice();
    svg.attr('aria-label', `${method.label}: ${label}. Use the shared time slider for readings.`);
    const grid = svg.append('g').attr('transform', `translate(${margin.left},0)`)
      .call(axisLeft(y).ticks(5).tickSize(-(width - margin.left - margin.right)).tickFormat(''));
    grid.select('.domain').remove();
    grid.selectAll('line').attr('opacity', .4);
    const event = scenario.event;
    if (event) svg.append('rect').attr('x', x(event.start_s)).attr('y', margin.top)
      .attr('width', x(event.end_s) - x(event.start_s)).attr('height', height - margin.bottom - margin.top)
      .attr('fill', 'var(--line)').attr('opacity', .25);
    const reference = kind === 'innovation' && mode === 'nis' ? scenario.diagnostics[method.id]?.dimension ?? 0 : 0;
    svg.append('line').attr('x1', margin.left).attr('x2', width - margin.right)
      .attr('y1', y(reference)).attr('y2', y(reference)).attr('stroke', 'var(--muted)').attr('stroke-dasharray', '4 4');
    if (kind === 'state' && points.length) {
      if (points[0].sigma !== null) {
        const band = area().curve(curveStepAfter).x(d => x(d.time))
          .y0(d => y(-2 * d.sigma)).y1(d => y(2 * d.sigma));
        svg.append('path').datum(points).attr('d', band).attr('fill', method.color).attr('opacity', .12);
        for (const sign of [-1, 1]) svg.append('path').datum(points)
          .attr('d', line().curve(curveStepAfter).x(d => x(d.time)).y(d => y(sign * 2 * d.sigma)))
          .attr('fill', 'none').attr('stroke', method.color).attr('stroke-dasharray', '3 3').attr('opacity', .6);
      }
      svg.append('path').datum(points).attr('d', line().curve(curveStepAfter).x(d => x(d.time)).y(d => y(d.error)))
        .attr('fill', 'none').attr('stroke', method.color).attr('stroke-width', 1.5);
    } else if (kind === 'innovation') {
      svg.append('g').selectAll('path').data(points).join('path')
        .attr('d', d => d.component === 0 ? 'M-2,0a2,2 0 1,0 4,0a2,2 0 1,0 -4,0' : 'M-2,-2L2,2M-2,2L2,-2')
        .attr('transform', d => `translate(${x(d.time)},${y(d.value)})`)
        .attr('fill', d => d.component === 0 ? method.color : 'none')
        .attr('stroke', d => d.component === 0 ? method.color : 'var(--muted)').attr('stroke-width', 1).attr('opacity', .8);
    }
    svg.append('g').attr('transform', `translate(0,${height - margin.bottom})`)
      .call(axisBottom(x).ticks(width < 420 ? 4 : 6).tickSizeOuter(0));
    svg.append('g').attr('transform', `translate(${margin.left},0)`).call(axisLeft(y).ticks(5).tickSizeOuter(0));
    svg.append('text').attr('class', 'axis-title').attr('x', margin.left).attr('y', 17).text(label);
    svg.append('text').attr('class', 'axis-title').attr('x', (width + margin.left) / 2).attr('y', height - 4).text('Time (s)');
    svg.append('line').attr('class', 'cursor').attr('y1', margin.top).attr('y2', height - margin.bottom)
      .attr('stroke', 'var(--accent)').attr('stroke-dasharray', '2 3');
    svg.append('g').attr('class', 'cursor-points');
    svg.append('rect').attr('x', margin.left).attr('y', margin.top)
      .attr('width', width - margin.left - margin.right).attr('height', height - margin.top - margin.bottom)
      .attr('fill', 'transparent').style('touch-action', 'pan-y').style('cursor', 'crosshair')
      .on('pointerdown', function(event) { holding = true; this.setPointerCapture(event.pointerId); inspect(event, true); })
      .on('pointermove', event => { if (event.pointerType === 'mouse' || holding) inspect(event, holding); })
      .on('pointerup pointercancel lostpointercapture', () => { holding = false; });
    setCursor(currentTime);
  }

  function inspect(event, pause) {
    onInspect(Math.max(0, Math.min(duration, x.invert(pointer(event, svg.node())[0]))), pause);
  }

  function setCursor(time) {
    currentTime = time;
    if (!scenario) return;
    svg.select('.cursor').attr('x1', x(time)).attr('x2', x(time));
    let selected = [];
    if (kind === 'state') {
      const sample = stateDiagnostic(diagnosticStateAt(scenario, time), method.id, component);
      if (sample) selected = [{ time: sample.time, value: sample.error }];
    } else {
      const row = lastInnovation(scenario, method.id, time);
      if (row) selected = mode === 'nis' ? [{ time: row[0], value: row.at(-1) }]
        : [{ time: row[0], value: row[1] }, ...(method.id === 'ekf' ? [{ time: row[0], value: row[2] }] : [])];
    }
    svg.select('.cursor-points').selectAll('circle').data(selected).join('circle')
      .attr('cx', d => x(d.time)).attr('cy', d => y(d.value)).attr('r', 4)
      .attr('fill', 'none').attr('stroke', method.color).attr('stroke-width', 1.5);
  }

  const observer = new ResizeObserver(draw);
  observer.observe(container);
  document.fonts?.ready.then(draw);
  return {
    setData(nextScenario, nextMethod, nextComponent, nextMode) {
      scenario = nextScenario; method = nextMethod; component = nextComponent; mode = nextMode; draw();
    },
    setCursor,
    destroy() { observer.disconnect(); svg.remove(); },
  };
}
