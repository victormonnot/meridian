import { axisBottom, axisLeft, scaleLinear, scalePoint, select } from 'd3';
import { trialStatistics } from './trials.js';

// Seed labels identify separate trials, not successive times or a replay schedule.
export function createTrialChart(container, readout) {
  const height = 270;
  const margin = { top: 28, right: 16, bottom: 44, left: 64 };
  const svg = select(container).append('svg').attr('role', 'group');
  let scenario, method, selectedSeed;

  function draw() {
    if (!scenario) return;
    const width = container.getBoundingClientRect().width;
    if (width < margin.left + margin.right + 20) return;
    const statistics = trialStatistics(scenario, method.id);
    const x = scalePoint().domain(statistics.seeds).range([margin.left, width - margin.right]).padding(.5);
    const y = scaleLinear().domain(statistics.domain).range([height - margin.bottom, margin.top]);
    svg.attr('viewBox', `0 0 ${width} ${height}`)
      .attr('aria-label', `${method.label}: full-run angle RMSE by noise seed. Focus or point at a dot to read its score. The dashed line is the displayed seed ${selectedSeed}.`);
    svg.selectAll('*').remove();
    const grid = svg.append('g').attr('aria-hidden', 'true').attr('transform', `translate(${margin.left},0)`)
      .call(axisLeft(y).ticks(5).tickSize(-(width - margin.left - margin.right)).tickFormat(''));
    grid.select('.domain').remove();
    grid.selectAll('line').attr('opacity', .4);
    svg.append('line').attr('class', 'trial-reference').attr('aria-hidden', 'true')
      .attr('x1', margin.left).attr('x2', width - margin.right)
      .attr('y1', y(statistics.selected)).attr('y2', y(statistics.selected))
      .attr('stroke', method.color).attr('stroke-width', 1.5).attr('stroke-dasharray', '6 4');
    const points = statistics.seeds.map((seed, index) => ({ seed, value: statistics.values[index] }));
    const markers = svg.append('g').selectAll('g').data(points).join('g')
      .attr('class', 'trial-point').attr('data-seed', point => point.seed)
      .attr('transform', point => `translate(${x(point.seed)},${y(point.value)})`)
      .attr('tabindex', 0).attr('role', 'img')
      .attr('aria-label', point => `Noise seed ${point.seed}: ${point.value.toFixed(6)} degrees angle RMSE.`)
      .on('pointerenter focus click', function (event, point) {
        markers.classed('is-inspected', item => item.seed === point.seed);
        readout.textContent = `Seed ${point.seed} · ${method.label} · ${point.value.toFixed(6)}° RMSE`;
      })
      .on('keydown', function (event, point) {
        const index = points.findIndex(item => item.seed === point.seed);
        const next = event.key === 'ArrowRight' ? Math.min(points.length - 1, index + 1)
          : event.key === 'ArrowLeft' ? Math.max(0, index - 1) : null;
        if (next !== null) { event.preventDefault(); markers.nodes()[next].focus(); }
      });
    markers.append('circle').attr('r', 12).attr('fill', 'transparent');
    markers.append('circle').attr('class', 'trial-dot').attr('r', 3.5)
      .attr('fill', method.color).attr('stroke', method.color);
    const step = Math.max(1, Math.ceil(points.length / (width < 420 ? 5 : 10)));
    const ticks = statistics.seeds.filter((seed, index) => index % step === 0 || index === points.length - 1);
    svg.append('g').attr('aria-hidden', 'true').attr('transform', `translate(0,${height - margin.bottom})`)
      .call(axisBottom(x).tickValues(ticks).tickSizeOuter(0));
    svg.append('g').attr('aria-hidden', 'true').attr('transform', `translate(${margin.left},0)`)
      .call(axisLeft(y).ticks(5).tickSizeOuter(0));
    svg.append('text').attr('aria-hidden', 'true').attr('class', 'axis-title').attr('x', margin.left).attr('y', 16).text('Angle RMSE (°)');
    svg.append('text').attr('aria-hidden', 'true').attr('class', 'axis-title')
      .attr('x', (margin.left + width - margin.right) / 2).attr('y', height - 4).attr('text-anchor', 'middle')
      .text('Noise seed · separate runs');
  }

  let observedWidth;
  let resizeFrame;
  const observer = new ResizeObserver(([entry]) => {
    if (entry.contentRect.width === observedWidth) return;
    observedWidth = entry.contentRect.width;
    // Drawing changes SVG height; defer it outside the resize notification.
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(draw);
  });
  observer.observe(container);
  return {
    setData(nextScenario, nextMethod, nextSeed) {
      scenario = nextScenario;
      method = nextMethod;
      selectedSeed = nextSeed;
      readout.textContent = `Displayed seed ${selectedSeed} · ${method.label} · ${scenario.metrics.rmse_deg[method.id].toFixed(6)}° RMSE`;
      draw();
    },
  };
}
