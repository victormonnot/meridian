from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("workspace", "0006_inputrevision_sensorartifact")]

    operations = [
        migrations.AddField(
            model_name="job",
            name="input_revision",
            field=models.ForeignKey(
                to="workspace.inputrevision", on_delete=django.db.models.deletion.SET_NULL,
                null=True, related_name="vio_jobs",
            ),
        ),
    ]
