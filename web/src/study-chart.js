import { axisBottom, axisLeft, format, scaleLinear, scalePoint, select } from 'd3';

const number = format('.6~g');
const tickNumber = format('.3~g');

// Each stem is one paired trial. Seed identifiers do not form a time series.
export function createStudyChart(container, readout) {
  const height = 282;
  const margin = { top: 30, right: 16, bottom: 46, left: 64 };
  const svg = select(container).append('svg').attr('role', 'group');
  const title = svg.append('title');
  const grid = svg.append('g').attr('aria-hidden', 'true');
  const zero = svg.append('line').attr('class', 'study-zero').attr('aria-hidden', 'true')
    .attr('stroke', 'var(--muted)').attr('stroke-width', 1).attr('stroke-dasharray', '4 4');
  const stems = svg.append('g').attr('aria-hidden', 'true');
  const points = svg.append('g');
  const xAxis = svg.append('g').attr('aria-hidden', 'true');
  const yAxis = svg.append('g').attr('aria-hidden', 'true');
  const yTitle = svg.append('text').attr('class', 'study-axis-label').attr('aria-hidden', 'true');
  const xTitle = svg.append('text').attr('class', 'study-axis-label').attr('aria-hidden', 'true');
  let model;
  let selectedSeed;
  let observedWidth;
  let resizeFrame;
  let destroyed = false;

  function valueLabel(value, signed = false) {
    const unit = model.unit === 'dimensionless' ? '' : model.unit;
    return `${signed && value > 0 ? '+' : ''}${number(value === 0 ? 0 : value)}${unit}`;
  }

  function describe(point) {
    return `Seed ${point.seed} · ${model.label} · ${model.settingLabel}: ${valueLabel(point.value)} · `
      + `${model.referenceLabel}: ${valueLabel(point.reference)} · Difference: ${valueLabel(point.delta, true)}`;
  }

  function showSelection() {
    points.selectAll('.study-point')
      .classed('is-inspected', point => point.seed === selectedSeed)
      .attr('tabindex', point => point.seed === selectedSeed ? 0 : -1)
      .attr('aria-pressed', point => String(point.seed === selectedSeed));
    points.selectAll('.study-dot')
      .attr('r', point => point.seed === selectedSeed ? 5 : 3.5)
      .attr('fill', point => point.seed === selectedSeed ? 'var(--bg)' : 'var(--ekf)')
      .attr('stroke-width', point => point.seed === selectedSeed ? 2 : 1);
    const selected = model.values.find(point => point.seed === selectedSeed);
    if (selected) readout.textContent = describe(selected);
  }

  function inspect(point) {
    selectedSeed = point.seed;
    showSelection();
    model.onSelect(point.seed);
  }

  function draw() {
    if (!model || destroyed) return;
    const width = container.getBoundingClientRect().width;
    // A hidden study panel is redrawn by the width observer when it is revealed.
    if (width < margin.left + margin.right + 20) return;
    const seeds = model.values.map(point => point.seed);
    const values = model.values.map(point => point.delta);
    const low = Math.min(0, ...values);
    const high = Math.max(0, ...values);
    const padding = (high - low || 0.01) * 0.12;
    const x = scalePoint().domain(seeds).range([margin.left, width - margin.right]).padding(0.5);
    const y = scaleLinear().domain([low - padding, high + padding]).nice()
      .range([height - margin.bottom, margin.top]);
    const description = `${model.label}: ${model.settingLabel} minus ${model.referenceLabel}, `
      + `one paired difference per noise seed. The dashed line marks zero difference. `
      + 'Use the arrow keys to inspect adjacent seeds.';
    svg.attr('viewBox', `0 0 ${width} ${height}`).attr('aria-label', description);
    title.text(description);
    grid.attr('transform', `translate(${margin.left},0)`)
      .call(axisLeft(y).ticks(5).tickSize(-(width - margin.left - margin.right)).tickFormat(''));
    grid.select('.domain').remove();
    grid.selectAll('line').attr('stroke', 'var(--line)').attr('opacity', 0.4);
    zero.attr('x1', margin.left).attr('x2', width - margin.right)
      .attr('y1', y(0)).attr('y2', y(0));
    stems.selectAll('line').data(model.values, point => point.seed).join('line')
      .attr('class', 'study-stem').attr('stroke', 'var(--muted)').attr('stroke-width', 1)
      .attr('opacity', 0.55).attr('x1', point => x(point.seed)).attr('x2', point => x(point.seed))
      .attr('y1', y(0)).attr('y2', point => y(point.delta));
    const markers = points.selectAll('.study-point').data(model.values, point => point.seed).join(
      enter => {
        const marker = enter.append('g').attr('class', 'study-point').attr('role', 'button');
        marker.append('circle').attr('r', 12).attr('fill', 'transparent');
        marker.append('circle').attr('class', 'study-dot').attr('stroke', 'var(--ekf)');
        return marker;
      },
    );
    markers.attr('data-seed', point => point.seed)
      .attr('transform', point => `translate(${x(point.seed)},${y(point.delta)})`)
      .attr('aria-label', point => describe(point))
      .on('pointerenter focus click', (event, point) => inspect(point))
      .on('keydown', function (event, point) {
        const index = seeds.indexOf(point.seed);
        const next = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? Math.min(seeds.length - 1, index + 1)
          : event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? Math.max(0, index - 1)
            : event.key === 'Home' ? 0 : event.key === 'End' ? seeds.length - 1 : null;
        if (next !== null) {
          event.preventDefault();
          markers.nodes()[next].focus();
        } else if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          inspect(point);
        }
      });
    const tickStep = Math.max(1, Math.ceil(seeds.length / (width < 420 ? 5 : 10)));
    const ticks = seeds.filter((seed, index) => index % tickStep === 0 || index === seeds.length - 1);
    xAxis.attr('transform', `translate(0,${height - margin.bottom})`)
      .call(axisBottom(x).tickValues(ticks).tickSizeOuter(0));
    yAxis.attr('transform', `translate(${margin.left},0)`)
      .call(axisLeft(y).ticks(5).tickFormat(tickNumber).tickSizeOuter(0));
    yTitle.attr('x', margin.left).attr('y', 16)
      .text(model.unit === 'dimensionless' ? 'Difference' : `Difference (${model.unit})`);
    xTitle.attr('x', (margin.left + width - margin.right) / 2).attr('y', height - 5)
      .attr('text-anchor', 'middle').text('Noise seed · separate runs');
    showSelection();
  }

  function scheduleDraw() {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(draw);
  }

  const observer = new ResizeObserver(([entry]) => {
    if (entry.contentRect.width === observedWidth) return;
    observedWidth = entry.contentRect.width;
    scheduleDraw();
  });
  observer.observe(container);
  document.fonts?.ready.then(() => { if (!destroyed) scheduleDraw(); });

  return {
    update(nextModel) {
      model = nextModel;
      selectedSeed = model.values.some(point => point.seed === model.selectedSeed)
        ? model.selectedSeed : model.values[0]?.seed;
      showSelection();
      draw();
    },
    resize: scheduleDraw,
    destroy() {
      destroyed = true;
      observer.disconnect();
      cancelAnimationFrame(resizeFrame);
      svg.remove();
    },
  };
}
