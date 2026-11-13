import { modulePageStub } from '@/components/ModuleNotice';
import { MODULES } from '@/services/modules';

export default modulePageStub({
  title: 'Warehouse',
  module: MODULES.WAREHOUSE,
  description:
    'Analyse experiments with assignment and metric data that stays in your own warehouse (BigQuery, Snowflake, Amazon Athena, Amazon Redshift).',
});
