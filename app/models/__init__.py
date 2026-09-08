from app.models.base import Base
from app.models.usuarios import Usuario, RolEnum
from app.models.clientes import Cliente, TipoPersonaEnum, ClienteDocumentoComercial
from app.models.proyectos import (
    Proyecto, ProyectoInfoTecnica,
    ProyectoInversor, ProyectoInversionista, Portafolio,
    ProyectoPendienteIgnorado,
)
from app.models.contactos import Contacto, ProyectoAreaContacto, TipoContactoEnum
from app.models.contratos import ContratoServicio, PPAContrato, PPATarifa, PPACompromisoEnergia, PPAResponsable
from app.models.fronteras import Frontera, FronteraQuoiaIgnorada
from app.models.contrato_frontera import ContratoFrontera
from app.models.alerta import Alerta
from app.models.operadores_red import OperadorRed, OperadorRedContacto
from app.models.fallas import (
    FallaCatCategoria, FallaCatTipo, FallaCatEstado,
    FallaCatPrioridad, FallaCatResolucion, Falla, FallaSeguimiento, FallaIntervalo,
    FallaInversor,
)
from app.models.liquidaciones import (
    Liquidacion, LiquidacionCosto,
    LiquidacionMandato, LiquidacionMandatoLinea, LiquidacionFactura,
)
from app.models.promotor import PromoterCatalogoRequisito, PromoterSeguimiento
from app.models.rec import RecProceso
from app.models.asic import AsicSolicitud, AsicCambioContrato, GesconDiccionario
from app.models.generacion import GeneracionDiaria
from app.models.gestion import GestionRegistro
from app.models.cumplimiento import CumplimientoMensual
from app.models.notificaciones import Notificacion, TipoNotificacionEnum
from app.models.costos_variables import CostoVariable
from app.models.polizas import Poliza
from app.models.starlink import StarlinkFactura, StarlinkMapeoSitio, StarlinkFacturaLinea
from app.models.informe_om import ProyectoInformeOM, EstadoInformeOMEnum
from app.models.panel_contable import (
    PanelContable, PanelContableLinea, TipoPanelEnum, GrupoLineaEnum,
    ClasificacionLiquidacion, TipoLiquidacionEnum, MapeoCeldaConcepto,
    AliasFuenteIngreso, PanelConsecutivo,
)
from app.models.mandatos import (
    Mandato, MandatoInversionista, EstadoMandatoCostoEnum,
)
from app.models.finanzas_mandatos import (
    FinanzasMandato, TipoMandatoEnum, EstadoFirmaEnum,
)
from app.models.om import IPCTasa, OMSeleccion, OMFacturaMensual, OMDocumentoProyecto
from app.models.arriendos import ArrProyecto, ArrIPCTasa, ArrSeleccion, ArrDocumento
from app.models.verificacion_costos import VerificacionCosto
from app.models.reporte_energia import ReporteEnergiaGeneracion, ReporteEnergiaConsumo
from app.models.retos import RetoTrimestre, RetoMetrica, RetoValorSemanal

__all__ = [
    "Base", "Usuario", "Cliente", "Proyecto", "ProyectoInfoTecnica",
    "ProyectoInversor", "Contacto", "ProyectoAreaContacto", "TipoContactoEnum",
    "ProyectoInversionista", "Portafolio", "ProyectoPendienteIgnorado",
    "ContratoServicio", "PPAContrato", "PPATarifa", "PPACompromisoEnergia", "PPAResponsable",
    "Frontera", "FronteraQuoiaIgnorada", "ContratoFrontera", "Alerta", "OperadorRed", "OperadorRedContacto",
    "FallaCatCategoria", "FallaCatTipo", "FallaCatEstado", "FallaCatPrioridad",
    "FallaCatResolucion", "Falla", "FallaSeguimiento", "FallaIntervalo", "FallaInversor",
    "Liquidacion", "LiquidacionCosto",
    "LiquidacionMandato", "LiquidacionMandatoLinea", "LiquidacionFactura",
    "PromoterCatalogoRequisito", "PromoterSeguimiento",
    "RecProceso", "AsicSolicitud", "AsicCambioContrato", "GesconDiccionario",
    "GeneracionDiaria",
    "GestionRegistro",
    "CumplimientoMensual",
    "Notificacion", "TipoNotificacionEnum",
    "CostoVariable",
    "Poliza",
    "StarlinkFactura", "StarlinkMapeoSitio", "StarlinkFacturaLinea",
    "ProyectoInformeOM", "EstadoInformeOMEnum",
    "PanelContable", "PanelContableLinea", "TipoPanelEnum", "GrupoLineaEnum",
    "ClasificacionLiquidacion", "TipoLiquidacionEnum", "MapeoCeldaConcepto",
    "AliasFuenteIngreso", "PanelConsecutivo",
    "Mandato", "MandatoInversionista", "EstadoMandatoCostoEnum",
    "IPCTasa", "OMSeleccion", "OMFacturaMensual", "OMDocumentoProyecto",
    "ArrProyecto", "ArrIPCTasa", "ArrSeleccion", "ArrDocumento",
    "VerificacionCosto",
    "ReporteEnergiaGeneracion", "ReporteEnergiaConsumo",
    "RetoTrimestre", "RetoMetrica", "RetoValorSemanal",
]
from app.models.clasificacion_energia import ClasificacionEnergiaMensual, CATEGORIAS_ENERGIA
from app.models.comercial import (
    Oportunidad, OportunidadEstadoHistorial, OportunidadGestion,
    EstadoOportunidadEnum, TipoGestionEnum,
)
from app.models.registros_cnd import (
    RegistroConexion, RegistroEtapa, RegistroTransicion, RegistroHito,
    RegistroParametros93, RegistroEquipoFrontera, RegistroDocumento, RegistroAlerta,
)
from app.models.garantias_proyecciones import GarantiaSnapshot, GarantiaPagado, BalCttosNeto  # noqa: F401
from app.models.garantias_modelo import (  # noqa: F401
    GarCalculo,
    GarComponentePred,
    GarComponenteReal,
    XMArchivo,
    XMMedida,
)
from app.models.garantias_ajustes import GarantiaAjuste, TipoAjusteEnum  # noqa: F401
from app.models.informes import InformeGuardado, TipoInformeEnum, EstadoInformeEnum  # noqa: F401
