import React from 'react';
import { useOutletContext } from 'react-router-dom';
import DataQualityWidget from '../components/DataQualityWidget.jsx';
import RiskControls from '../components/RiskControls.jsx';
import RiskGauges from '../components/RiskGauges.jsx';
import ScenarioStress from '../components/ScenarioStress.jsx';

export default function Risk() {
  const { config, performance, updateConfig } = useOutletContext();
  return (
    <div className="grid">
      <RiskGauges risk={config.risk} performance={performance} />
      <RiskControls
        value={config.risk}
        onChange={(risk) => updateConfig({ risk })}
      />
      <DataQualityWidget />
      <ScenarioStress />
    </div>
  );
}
