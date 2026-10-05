import { axisBottom, axisLeft, scaleLinear, scalePoint, select } from 'd3';
import { trialStatistics } from './trials.js';

// Seed labels identify separate trials, not successive times or a replay schedule.
export function createTrialChart(container, readout) {
  const height = 270;
  const margin = { top: 28, right: 16, bottom: 44, left: 64 };
  const svg = select(container).append('svg').attr('role', 'group');
  const grid = svg.append('g').attr('class', 'chart-grid').attr('aria-hidden', 'true');
  const reference = svg.append('line').attr('class', 'trial-reference').attr('aria-hidden', 'true');
  const points = svg.append('g');
  const xAxis = svg.append('g').attr('aria-hidden', 'true');
  const yAxis = svg.append('g').attr('aria-hidden', 'true');
  const yTitle = svg.append('text').attr('aria-hidden', 'true').attr('class', 'axis-title');
  const xTitle = svg.append('text').attr('aria-hidden', 'true').attr('class', 'axis-title');
  let scenario, method, selectedSeed;
  let inspectedSeed = null;

  function draw() {
    if (!scenario) return;
    const width = container.getBoundingClientRect().width;
    if (width < margin.left + margin.right + 20) return;
    const statistics = trialStatistics(scenario, method.id);
    const x = scalePoint().domain(statistics.seeds).range([margin.left, width - margin.right]).padding(.5);
    const y = scaleLinear().domain(statistics.domain).range([height - margin.bottom, margin.top]);
    svg.attr('viewBox', `0 0 ${width} ${height}`)
      .attr('aria-label', `${method.label}: full-run angle RMSE by noise seed. Use arrow keys to inspect adjacent seeds, or Home and End for the first and last. The dashed line is the displayed seed ${selectedSeed}.`);
    grid.attr('transform', `translate(${margin.left},0)`)
      .call(axisLeft(y).ticks(5).tickSize(-(width - margin.left - margin.right)).tickFormat(''));
    grid.select('.domain').remove();
    grid.selectAll('line').attr('opacity', .4);
    reference.attr('x1', margin.left).attr('x2', width - margin.right)
      .attr('y1', y(statistics.selected)).attr('y2', y(statistics.selected))
      .attr('stroke', method.color).attr('stroke-width', 1.5).attr('stroke-dasharray', '6 4');
    const values = statistics.seeds.map((seed, index) => ({ seed, value: statistics.values[index] }));
    // Keep the marker nodes so resizing preserves keyboard focus and inspection.
    const markers = points.selectAll('.trial-point').data(values, point => point.seed).join(enter => {
      const marker = enter.append('g').attr('class', 'trial-point').attr('role', 'button');
      marker.append('circle').attr('r', 12).attr('fill', 'transparent');
      marker.append('circle').attr('class', 'trial-dot').attr('r', 3.5);
      return marker;
    });

    function showInspection() {
      markers.classed('is-inspected', point => point.seed === inspectedSeed)
        .attr('tabindex', point => point.seed === (inspectedSeed ?? values[0].seed) ? 0 : -1)
        .attr('aria-pressed', point => String(point.seed === inspectedSeed));
      const inspected = values.find(point => point.seed === inspectedSeed);
      if (inspected) readout.textContent = `Seed ${inspected.seed} · ${method.label} · ${inspected.value.toFixed(6)}° RMSE`;
    }

    markers.attr('data-seed', point => point.seed)
      .attr('transform', point => `translate(${x(point.seed)},${y(point.value)})`)
      .attr('aria-label', point => `Noise seed ${point.seed}: ${point.value.toFixed(6)} degrees angle RMSE.`)
      .on('pointerenter focus click', function (event, point) {
        inspectedSeed = point.seed;
        showInspection();
      })
      .on('keydown', function (event, point) {
        const index = values.findIndex(item => item.seed === point.seed);
        const next = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? Math.min(values.length - 1, index + 1)
          : event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? Math.max(0, index - 1)
            : event.key === 'Home' ? 0 : event.key === 'End' ? values.length - 1 : null;
        if (next !== null) {
          event.preventDefault();
          inspectedSeed = values[next].seed;
          showInspection();
          markers.nodes()[next].focus();
        }
        else if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          inspectedSeed = point.seed;
          showInspection();
        }
      });
    markers.select('.trial-dot').attr('fill', method.color).attr('stroke', method.color);
    showInspection();
    const step = Math.max(1, Math.ceil(values.length / (width < 420 ? 5 : 10)));
    const ticks = statistics.seeds.filter((seed, index) => index % step === 0 || index === values.length - 1);
    xAxis.attr('transform', `translate(0,${height - margin.bottom})`)
      .call(axisBottom(x).tickValues(ticks).tickSizeOuter(0));
    yAxis.attr('transform', `translate(${margin.left},0)`)
      .call(axisLeft(y).ticks(5).tickSizeOuter(0));
    yTitle.attr('x', margin.left).attr('y', 16).text('Angle RMSE (°)');
    xTitle.attr('x', (margin.left + width - margin.right) / 2).attr('y', height - 4).attr('text-anchor', 'middle')
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
      inspectedSeed = null;
      readout.textContent = `Displayed seed ${selectedSeed} · ${method.label} · ${scenario.metrics.rmse_deg[method.id].toFixed(6)}° RMSE`;
      draw();
    },
  };
}
